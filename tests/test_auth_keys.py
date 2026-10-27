"""Two keys, strict checks (ticket 3).

The phone key (``ANOTHER_BRIDGE_PHONE_KEY``) and the AnotherAgent key
(``ANOTHER_CODER_API_KEY``) are compared as bytes, so a header with
non-ASCII characters is a plain mismatch instead of a 500, and the boot
check rejects short or shared keys.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth

PHONE = "p" * 40
AGENT = "a" * 40


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", PHONE)
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", AGENT)


def test_each_key_matches_only_its_own_surface() -> None:
    assert auth.key_matches(auth.PHONE, PHONE)
    assert auth.key_matches(auth.AGENT, AGENT)
    assert not auth.key_matches(auth.PHONE, AGENT)
    assert not auth.key_matches(auth.AGENT, PHONE)


def test_bytes_and_missing_values() -> None:
    assert auth.key_matches(auth.PHONE, PHONE.encode())
    assert not auth.key_matches(auth.PHONE, None)
    assert not auth.key_matches(auth.PHONE, "")


def test_non_ascii_value_is_a_mismatch_not_an_error() -> None:
    assert not auth.key_matches(auth.PHONE, "p" * 39 + "é")
    assert not auth.key_matches(auth.PHONE, ("p" * 39 + "é").encode("latin-1"))


def test_unset_surface_never_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", "")
    assert not auth.key_matches(auth.PHONE, "")
    assert not auth.key_matches(auth.PHONE, PHONE)


def test_validate_keys_accepts_two_long_distinct_keys() -> None:
    auth.validate_keys()


def test_validate_keys_accepts_one_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "")
    auth.validate_keys()


@pytest.mark.parametrize("name", ["ANOTHER_BRIDGE_PHONE_KEY", "ANOTHER_CODER_API_KEY"])
def test_validate_keys_rejects_short_keys(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "short-key")
    with pytest.raises(auth.KeyConfigError, match=name):
        auth.validate_keys()


def test_validate_keys_rejects_the_same_value_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", PHONE)
    with pytest.raises(auth.KeyConfigError, match="differ"):
        auth.validate_keys()


def test_validate_keys_rejects_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", "")
    monkeypatch.setenv("ANOTHER_CODER_API_KEY", "")
    with pytest.raises(auth.KeyConfigError):
        auth.validate_keys()


def test_validate_keys_never_echoes_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", "tiny-secret")
    with pytest.raises(auth.KeyConfigError) as exc:
        auth.validate_keys()
    assert "tiny-secret" not in str(exc.value)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/phone", dependencies=[Depends(auth.verify_phone_key)])
    def phone() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/agent", dependencies=[Depends(auth.verify_api_key)])
    def agent() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/either", dependencies=[Depends(auth.verify_any_key)])
    def either() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_dependencies_keep_the_surfaces_apart(client: TestClient) -> None:
    assert client.get("/phone", headers={"X-Coder-Key": PHONE}).status_code == 200
    assert client.get("/phone", headers={"X-Coder-Key": AGENT}).status_code == 401
    assert client.get("/agent", headers={"X-Coder-Key": AGENT}).status_code == 200
    assert client.get("/agent", headers={"X-Coder-Key": PHONE}).status_code == 401


def test_auth_verify_style_route_takes_either_key(client: TestClient) -> None:
    # /auth/verify serves the phone and the AnotherAgent Connect dialog,
    # so it accepts both keys (no AnotherAgent change needed).
    assert client.get("/either", headers={"X-Coder-Key": PHONE}).status_code == 200
    assert client.get("/either", headers={"X-Coder-Key": AGENT}).status_code == 200
    assert client.get("/either", headers={"X-Coder-Key": "nope"}).status_code == 401


def test_non_ascii_header_gets_401_not_500(client: TestClient) -> None:
    for raw in (("p" * 39 + "é").encode("utf-8"), ("p" * 39 + "é").encode("latin-1")):
        resp = client.get("/phone", headers={"X-Coder-Key": raw})
        assert resp.status_code == 401
