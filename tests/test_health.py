"""Coverage for GET /health.

The route is unauthed (ngrok / uptime checks consume it without the
bridge secret) and surfaces a small diagnostic payload that bug
reports + remote-deploy operators reference. Lock the response shape
so a future refactor can't silently drop a field consumers depend on.

Tests build a minimal FastAPI app around the router instead of
importing ``main.app`` — that would trigger the lifespan claude probe
+ reaper thread on every test, which adds latency and would couple
this file to unrelated boot behavior. The trade-off is that
``app.state.claude_probe`` isn't auto-populated; we set it explicitly
when the test cares about that branch.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.health import router


def _build_client(claude_probe: dict | None = None) -> TestClient:
    """Fresh app per test so app.state can vary between cases without
    leaking. Setting ``claude_probe`` simulates what main.py's
    lifespan would have populated at boot."""
    app = FastAPI()
    app.include_router(router)
    if claude_probe is not None:
        app.state.claude_probe = claude_probe
    return TestClient(app)


class TestHealth:
    def test_returns_baseline_fields(self) -> None:
        # ok + service + version are always present regardless of
        # lifespan state. Uptime monitors key on these.
        resp = _build_client().get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "another_bridge"
        # Don't pin the literal — pyproject.toml is the source of
        # truth and this test should keep passing across bumps.
        assert isinstance(body["version"], str)
        assert body["version"]

    def test_version_falls_back_when_package_not_installed(self) -> None:
        # If the bridge runs from a clone without `pip install`,
        # importlib.metadata raises PackageNotFoundError. /health
        # must stay 200 — uptime monitors don't tolerate flapping.
        with patch(
            "routers.health._pkg_version",
            side_effect=PackageNotFoundError("another_bridge"),
        ):
            resp = _build_client().get("/health")
        assert resp.status_code == 200
        assert resp.json()["version"] == "unknown"

    def test_claude_probe_defaults_when_lifespan_skipped(self) -> None:
        # No lifespan = no app.state.claude_probe = report it as
        # ``probe not run`` instead of fabricating a green result.
        # This is the test-runner case AND any deploy that bypasses
        # main.app (uvicorn pointed at routers.health for some reason).
        body = _build_client().get("/health").json()
        assert body["claude_probe"] == {"ok": False, "detail": "probe not run"}

    def test_claude_probe_surfaces_lifespan_state_ok(self) -> None:
        # Healthy production case: lifespan ran probe_claude_binary,
        # got ok=True with the version detail, stashed it on
        # app.state. /health must echo it verbatim so operators can
        # `curl /health | jq .claude_probe` to triage remote deploys.
        client = _build_client(
            claude_probe={"ok": True, "detail": "claude-code 1.2.3"},
        )
        body = client.get("/health").json()
        assert body["claude_probe"] == {"ok": True, "detail": "claude-code 1.2.3"}

    def test_claude_probe_surfaces_lifespan_state_not_ok(self) -> None:
        # Unhealthy case: probe failed at boot (NVM PATH, expired
        # auth, missing binary). The detail string is what the
        # operator sees — should pass through unchanged so the same
        # message they'd see in `docker logs` shows up via curl.
        client = _build_client(
            claude_probe={
                "ok": False,
                "detail": "`claude` not found on PATH. Set CLAUDE_BIN_PATH...",
            },
        )
        body = client.get("/health").json()
        assert body["claude_probe"]["ok"] is False
        assert "CLAUDE_BIN_PATH" in body["claude_probe"]["detail"]

    def test_session_store_reachable_true_in_healthy_setup(self) -> None:
        # Default conftest provides an in-memory SQLite session_store
        # that's healthy. Verifies the field comes through as True
        # under normal circumstances — the inverse of the failure
        # test below.
        body = _build_client().get("/health").json()
        assert body["session_store_reachable"] is True

    def test_session_store_reachable_false_when_probe_raises(self) -> None:
        # Patch the store's reachability probe to simulate a locked /
        # corrupted DB. /health must report False, not 500. This is
        # the case the field exists FOR — a remote operator notices
        # the field flipped and knows to look at SQLite.
        with patch(
            "routers.health.session_store.is_reachable", return_value=False
        ):
            body = _build_client().get("/health").json()
        assert body["session_store_reachable"] is False
        # Top-level "ok" stays True because the bridge process itself
        # is alive — uptime monitors stay green; only the structured
        # diagnostic flips.
        assert body["ok"] is True
