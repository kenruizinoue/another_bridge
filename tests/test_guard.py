"""The ASGI guard in front of every route (tickets 2 and 3).

It checks the key for the route's surface from the headers alone, so an
unauthenticated body is never read, caps request sizes, and slows down
an address that keeps sending wrong keys.
"""

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.guard import GuardMiddleware, client_ip, surface_for

PHONE = "p" * 40
AGENT = "a" * 40


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", PHONE)
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", AGENT)


def _app(**guard_kwargs) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/auth/verify")
    def verify() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sessions")
    def sessions() -> list:
        return []

    @app.post("/sessions/{sid}/resume")
    async def resume(sid: str, request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/tts")
    async def tts(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.get("/files/roots")
    def roots() -> dict:
        return {"roots": []}

    @app.post("/tools/list_repos")
    async def list_repos(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_middleware(GuardMiddleware, **guard_kwargs)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


# ── Surfaces ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "surface"),
    [
        ("/health", "open"),
        ("/sessions", "phone"),
        ("/sessions/abc/resume/stream", "phone"),
        ("/tts", "phone"),
        ("/transcribe", "phone"),
        ("/files/raw", "phone"),
        ("/auth/verify", "any"),
        ("/chat/stream", "agent"),
        ("/tools/instruct_planning", "agent"),
        ("/jobs/1/status", "agent"),
        ("/docs", "agent"),
        ("/openapi.json", "agent"),
        ("/sessionsx", "agent"),
        ("/healthz", "agent"),
        ("/", "agent"),
    ],
)
def test_surface_for(path: str, surface: str) -> None:
    assert surface_for(path) == surface


def test_health_stays_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_phone_key_opens_phone_routes_only(client: TestClient) -> None:
    phone = {"X-Coder-Key": PHONE}
    assert client.get("/sessions", headers=phone).status_code == 200
    assert client.get("/files/roots", headers=phone).status_code == 200
    assert client.post("/tts", headers=phone, content=b"{}").status_code == 200
    resp = client.post("/tools/list_repos", headers=phone, content=b"{}")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid or missing X-Coder-Key", "code": "unauthorized"}


def test_agent_key_opens_agent_routes_only(client: TestClient) -> None:
    agent = {"X-Coder-Key": AGENT}
    assert client.post("/tools/list_repos", headers=agent, content=b"{}").status_code == 200
    assert client.get("/sessions", headers=agent).status_code == 401
    assert client.get("/files/roots", headers=agent).status_code == 401


def test_auth_verify_takes_either_key(client: TestClient) -> None:
    assert client.get("/auth/verify", headers={"X-Coder-Key": PHONE}).status_code == 200
    assert client.get("/auth/verify", headers={"X-Coder-Key": AGENT}).status_code == 200
    assert client.get("/auth/verify").status_code == 401


def test_unknown_routes_and_docs_need_the_agent_key(client: TestClient) -> None:
    assert client.get("/docs").status_code == 401
    assert client.get("/nope").status_code == 401
    assert client.get("/nope", headers={"X-Coder-Key": AGENT}).status_code == 404


def test_surface_without_a_key_stays_closed(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", "")
    resp = client.get("/sessions", headers={"X-Coder-Key": ""})
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"
    # The agent surface is unaffected.
    assert client.post("/tools/list_repos", headers={"X-Coder-Key": AGENT}, content=b"{}").status_code == 200


def test_non_ascii_key_gets_401(client: TestClient) -> None:
    for raw in (("p" * 39 + "é").encode("utf-8"), ("p" * 39 + "é").encode("latin-1")):
        assert client.get("/sessions", headers={"X-Coder-Key": raw}).status_code == 401


def test_head_and_options_follow_the_same_rules(client: TestClient) -> None:
    assert client.head("/sessions").status_code == 401
    assert client.options("/sessions").status_code == 401


# ── The body is never read before the key check ─────────────────────


def _run_asgi(app, scope_overrides: dict, body_chunks: list[bytes]) -> tuple[int, int]:
    """Call the app as a server would. Returns (status, chunks read)."""
    reads = 0
    status = 0
    chunks = list(body_chunks)

    async def receive():
        nonlocal reads
        reads += 1
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/sessions/abc/resume",
        "raw_path": b"/sessions/abc/resume",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("203.0.113.9", 5000),
        "server": ("127.0.0.1", 8000),
    }
    scope.update(scope_overrides)
    asyncio.run(app(scope, receive, send))
    return status, reads


def test_unauthenticated_post_is_rejected_without_reading_the_body() -> None:
    big = [b"x" * (1024 * 1024)] * 200  # a 200 MB body, streamed
    status, reads = _run_asgi(_app(), {}, big)
    assert status == 401
    assert reads == 0


def test_oversize_content_length_is_rejected_without_reading_the_body() -> None:
    headers = [(b"x-coder-key", PHONE.encode()), (b"content-length", str(41 * 1024 * 1024).encode())]
    status, reads = _run_asgi(_app(), {"headers": headers}, [b"x"])
    assert status == 413
    assert reads == 0


# ── Size caps ───────────────────────────────────────────────────────


def test_resume_body_under_its_cap_passes(client: TestClient) -> None:
    resp = client.post("/sessions/abc/resume", headers={"X-Coder-Key": PHONE}, content=b"x" * 100_000)
    assert resp.status_code == 200
    assert resp.json() == {"bytes": 100_000}


def test_small_route_body_over_64kb_gets_413(client: TestClient) -> None:
    resp = client.post("/tts", headers={"X-Coder-Key": PHONE}, content=b"x" * (64 * 1024 + 1))
    assert resp.status_code == 413
    assert resp.json()["code"] == "body_too_large"


def test_resume_body_over_its_cap_gets_413() -> None:
    client = TestClient(_app(large_body_bytes=1000, small_body_bytes=100))
    resp = client.post("/sessions/abc/resume", headers={"X-Coder-Key": PHONE}, content=b"x" * 1001)
    assert resp.status_code == 413


def test_chunked_body_without_length_is_cut_at_the_cap() -> None:
    app = _app(large_body_bytes=1000, small_body_bytes=100)
    headers = [(b"x-coder-key", PHONE.encode())]
    status, reads = _run_asgi(app, {"headers": headers}, [b"x" * 600, b"x" * 600, b"x" * 600, b"x" * 600])
    assert status == 413
    assert reads == 2  # stopped as soon as the cap was crossed


def test_chunked_body_under_the_cap_passes() -> None:
    app = _app(large_body_bytes=1000, small_body_bytes=100)
    headers = [(b"x-coder-key", PHONE.encode())]
    status, _ = _run_asgi(app, {"headers": headers}, [b"x" * 400, b"x" * 400])
    assert status == 200


# ── Failed-auth throttle ────────────────────────────────────────────


def test_ten_failures_in_a_minute_get_429_even_with_the_right_key() -> None:
    clock = [1000.0]
    client = TestClient(_app(clock=lambda: clock[0]))
    for _ in range(10):
        assert client.get("/sessions", headers={"X-Coder-Key": "wrong"}).status_code == 401
    resp = client.get("/sessions", headers={"X-Coder-Key": PHONE})
    assert resp.status_code == 429
    assert resp.json()["code"] == "too_many_failed_attempts"
    # /health still answers.
    assert client.get("/health").status_code == 200
    # A minute later the address is let through again.
    clock[0] += 61
    assert client.get("/sessions", headers={"X-Coder-Key": PHONE}).status_code == 200


def test_successful_requests_do_not_count_as_failures() -> None:
    client = TestClient(_app(clock=lambda: 1000.0))
    for _ in range(30):
        assert client.get("/sessions", headers={"X-Coder-Key": PHONE}).status_code == 200


def test_client_ip_trusts_forwarded_for_only_from_localhost() -> None:
    forwarded = [(b"x-forwarded-for", b"198.51.100.1, 203.0.113.7")]
    # ngrok connects from localhost and appends the real client last.
    assert client_ip({"client": ("127.0.0.1", 1), "headers": forwarded}) == "203.0.113.7"
    assert client_ip({"client": ("::1", 1), "headers": forwarded}) == "203.0.113.7"
    # Anyone else cannot pick their own address.
    assert client_ip({"client": ("192.0.2.5", 1), "headers": forwarded}) == "192.0.2.5"
    assert client_ip({"client": ("127.0.0.1", 1), "headers": []}) == "127.0.0.1"
    assert client_ip({"client": None, "headers": []}) == "unknown"


def test_failures_behind_ngrok_are_counted_per_real_client() -> None:
    clock = [1000.0]
    app = _app(clock=lambda: clock[0])

    def call(ip: str, key: str) -> int:
        headers = [(b"x-coder-key", key.encode()), (b"x-forwarded-for", ip.encode())]
        status, _ = _run_asgi(
            app,
            {"method": "GET", "path": "/sessions", "raw_path": b"/sessions", "headers": headers, "client": ("127.0.0.1", 1)},
            [],
        )
        return status

    for _ in range(10):
        assert call("203.0.113.7", "wrong") == 401
    assert call("203.0.113.7", PHONE) == 429
    assert call("198.51.100.20", PHONE) == 200
