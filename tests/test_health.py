"""Coverage for GET /health.

The route is unauthed (ngrok / uptime checks consume it without the
bridge secret) and surfaces a version string that bug reports
reference. Lock the response shape so a future refactor can't
silently drop a field operators depend on.

Tests build a minimal FastAPI app around the router instead of
importing ``main.app`` — that would trigger the lifespan claude probe
+ reaper thread on every test, which adds latency and would couple
this file to unrelated boot behavior.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.health import router


_app = FastAPI()
_app.include_router(router)
client = TestClient(_app)


class TestHealth:
    def test_returns_ok_service_and_version(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "another_coder"
        # importlib.metadata returns the installed package version.
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
            side_effect=PackageNotFoundError("another_coder"),
        ):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["version"] == "unknown"
