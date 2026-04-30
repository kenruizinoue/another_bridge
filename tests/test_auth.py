"""Header auth tests for the bridge.

Mounts a tiny FastAPI app with verify_api_key as a dependency on a stub
route, drives it through TestClient, and asserts the four shapes that
matter for the contract:
  - missing header     -> 401
  - wrong header value -> 401
  - matching header    -> 200
  - missing env var    -> 503

Doing it on a stub route (instead of the real /chat/stream) keeps the
test fast and isolates auth from Claude-spawn / streaming concerns.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth import verify_api_key


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(verify_api_key)])
    def protected() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_missing_header_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "expected-secret")
    resp = client.get("/protected")
    assert resp.status_code == 401
    assert "X-Coder-Key" in resp.json()["detail"]


def test_wrong_header_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "expected-secret")
    resp = client.get("/protected", headers={"X-Coder-Key": "wrong"})
    assert resp.status_code == 401


def test_matching_header_passes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "expected-secret")
    resp = client.get("/protected", headers={"X-Coder-Key": "expected-secret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}


def test_unset_env_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty env var = misconfigured deploy. Refusing rather than allowing
    # all is the whole point of this dep — assert that loudly.
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "")
    resp = client.get("/protected", headers={"X-Coder-Key": "anything"})
    assert resp.status_code == 503
