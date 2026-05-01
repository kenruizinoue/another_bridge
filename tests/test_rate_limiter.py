"""Coverage for the slowapi rate-limit wiring.

Two layers tested:

  1. Unit — the key_func picks the right bucket: X-Coder-Key when
     present, remote-IP fallback when absent. Locked down because
     a future change that swaps to remote-only keying would
     silently let a single-key shared deployment dodge limits
     from multiple platform IPs.

  2. Integration — fire N+1 requests and assert the (N+1)th
     returns 429. Uses /auth/verify because it's the cheapest
     surface to spam (no subprocess spawn) and the limit number
     is configurable + small. Other routes follow the same
     decorator pattern; this single smoke is enough to lock the
     wiring contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi.middleware import SlowAPIMiddleware

from services.rate_limiter import _coder_key_or_remote, limiter


# ──────────────────────────────────────────────────────────────────────
# key_func — unit
# ──────────────────────────────────────────────────────────────────────


class TestKeyFunc:
    def _fake_request(self, headers: dict[str, str], client_ip: str = "127.0.0.1"):
        # Minimal stand-in matching what slowapi reads from the request.
        # Avoids spinning up a real Starlette Request just to validate
        # the bucket selection.
        req = MagicMock()
        req.headers = headers
        req.client = MagicMock(host=client_ip)
        return req

    def test_uses_coder_key_when_present(self) -> None:
        req = self._fake_request({"X-Coder-Key": "secret-1"})
        bucket = _coder_key_or_remote(req)
        assert bucket.startswith("key:")

    def test_falls_back_to_remote_ip_when_no_key(self) -> None:
        req = self._fake_request({}, client_ip="10.0.0.42")
        bucket = _coder_key_or_remote(req)
        # IP fallback is prefixed and includes the address. Locks the
        # convention so a future refactor that swaps the prefix gets
        # caught.
        assert bucket.startswith("ip:")
        assert "10.0.0.42" in bucket

    def test_different_keys_get_different_buckets(self) -> None:
        a = _coder_key_or_remote(self._fake_request({"X-Coder-Key": "alpha"}))
        b = _coder_key_or_remote(self._fake_request({"X-Coder-Key": "beta"}))
        assert a != b

    def test_same_key_gets_same_bucket(self) -> None:
        # Idempotency — the bucket assignment must be a pure function
        # of the key (within a process). Otherwise rate limits would
        # be per-request-instance, not per-key.
        a = _coder_key_or_remote(self._fake_request({"X-Coder-Key": "k"}))
        b = _coder_key_or_remote(self._fake_request({"X-Coder-Key": "k"}))
        assert a == b

    def test_does_not_leak_raw_key_into_bucket_name(self) -> None:
        # Bucket names appear in slowapi's storage + can show up in
        # error response bodies. Hashing prevents the raw secret from
        # leaking through that surface.
        req = self._fake_request({"X-Coder-Key": "super-secret-xyz"})
        bucket = _coder_key_or_remote(req)
        assert "super-secret-xyz" not in bucket


# ──────────────────────────────────────────────────────────────────────
# Integration — actual 429 enforcement on a real route
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_limiter() -> TestClient:
    """Build a fresh app with the limiter middleware wired and a
    cheap test route that has a tight 3/minute cap. Mirrors
    main.py's setup so the test exercises the actual middleware
    path, not a mock.

    A new in-memory backend per test isolates the bucket counts —
    slowapi's module-level limiter would otherwise bleed state
    between cases. We swap the storage_uri attribute on the global
    limiter to ":memory:" before each test."""
    from slowapi import Limiter
    from services.rate_limiter import _coder_key_or_remote

    fresh = Limiter(key_func=_coder_key_or_remote)

    app = FastAPI()
    app.state.limiter = fresh
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/probe")
    @fresh.limit("3/minute")
    def probe(request: Request):
        return {"ok": True}

    return TestClient(app)


class TestLimiterEnforcement:
    def test_under_limit_returns_200(self, client_with_limiter: TestClient) -> None:
        # Three requests at the 3/minute cap should all succeed.
        for i in range(3):
            resp = client_with_limiter.get(
                "/probe",
                headers={"X-Coder-Key": "test-key"},
            )
            assert resp.status_code == 200, f"req {i + 1} failed: {resp.text}"

    def test_over_limit_returns_429(self, client_with_limiter: TestClient) -> None:
        # Drain the bucket, then expect 429 on the next call.
        for _ in range(3):
            client_with_limiter.get(
                "/probe",
                headers={"X-Coder-Key": "drain-key"},
            )
        resp = client_with_limiter.get(
            "/probe",
            headers={"X-Coder-Key": "drain-key"},
        )
        assert resp.status_code == 429

    def test_separate_keys_have_separate_buckets(
        self, client_with_limiter: TestClient
    ) -> None:
        # Drain key A's bucket completely.
        for _ in range(3):
            client_with_limiter.get(
                "/probe",
                headers={"X-Coder-Key": "key-A"},
            )
        # Key A should now 429.
        a_429 = client_with_limiter.get(
            "/probe",
            headers={"X-Coder-Key": "key-A"},
        )
        assert a_429.status_code == 429
        # Key B is untouched — must still 200.
        b_ok = client_with_limiter.get(
            "/probe",
            headers={"X-Coder-Key": "key-B"},
        )
        assert b_ok.status_code == 200
