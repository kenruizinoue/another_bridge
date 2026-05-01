"""HTTP-level tests for the cancel endpoint via fastapi.testclient.

The companion test_jobs_router_cancel.py calls the route function
directly — fast and dependency-free, but doesn't exercise the actual
FastAPI router (URL pattern matching, HTTP method enforcement, JSON
serialization, status codes). These tests close that seam by mounting
the router on a TestClient and hitting it over a real HTTP transport.

Pairs with the manual cancel verified in production: when the platform
fires POST /jobs/<id>/cancel after a chat-cancel, this layer is what
turns the URL into the JobManager.cancel call.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobs import job_manager
from routers import jobs as jobs_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(jobs_router.router)
    return TestClient(app)


# _clean_jobs autouse fixture now lives in tests/conftest.py — see
# the comment in test_chat_polling.py for context.


# ──────────────────────────────────────────────────────────────────────
# Routing + method enforcement
# ──────────────────────────────────────────────────────────────────────


class TestRouting:
    def test_post_jobs_cancel_returns_200_for_running_job(self, client: TestClient) -> None:
        job = job_manager.create(kind="test")
        resp = client.post(f"/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"job_id": job.job_id, "cancelled": True}
        # Side effect through the real HTTP path — same outcome as the
        # direct-call test, just via the router.
        assert job_manager.is_cancelled(job.job_id) is True

    def test_post_jobs_cancel_returns_200_for_unknown_job(self, client: TestClient) -> None:
        resp = client.post("/jobs/no-such-id/cancel")
        assert resp.status_code == 200
        assert resp.json() == {
            "job_id": "no-such-id",
            "cancelled": False,
            "reason": "not found",
        }

    def test_get_jobs_cancel_is_405(self, client: TestClient) -> None:
        # /cancel is POST-only — a GET must be method-not-allowed, not
        # silently routed somewhere unexpected. Belt + suspenders for
        # the platform contract: the platform fires POST, and accidental
        # GETs (curl misuse, browser preview) shouldn't trigger a real
        # cancel.
        job = job_manager.create(kind="test")
        resp = client.get(f"/jobs/{job.job_id}/cancel")
        assert resp.status_code == 405
        # Cancel did NOT fire just because a GET hit the URL.
        assert job_manager.is_cancelled(job.job_id) is False

    def test_post_jobs_status_does_not_route_to_cancel(self, client: TestClient) -> None:
        # Sanity: /status and /cancel are siblings under the same path
        # prefix. A POST to /status must not accidentally trigger cancel
        # (and vice versa). Locks the route segregation.
        job = job_manager.create(kind="test")
        resp = client.post(f"/jobs/{job.job_id}/status")
        assert resp.status_code == 200
        # Status response shape — and crucially, not cancelled.
        body = resp.json()
        assert body["status"] == "running"
        assert job_manager.is_cancelled(job.job_id) is False


# ──────────────────────────────────────────────────────────────────────
# Response shape over the wire
# ──────────────────────────────────────────────────────────────────────


class TestResponseShape:
    def test_cancel_response_is_application_json(self, client: TestClient) -> None:
        job = job_manager.create(kind="test")
        resp = client.post(f"/jobs/{job.job_id}/cancel")
        assert resp.headers["content-type"].startswith("application/json")

    def test_cancel_response_uses_job_id_from_url_path(self, client: TestClient) -> None:
        # Echoes the path param back into the response body — a regression
        # would silently make the platform's cancel-job correlation
        # impossible without falling back to logs.
        job = job_manager.create(kind="test")
        resp = client.post(f"/jobs/{job.job_id}/cancel")
        assert resp.json()["job_id"] == job.job_id

    def test_unknown_job_response_includes_reason_field(self, client: TestClient) -> None:
        # The "not found" reason is what tells the operator (or the
        # platform's error-trace) that the cancel went to a stale id,
        # not that the cancel logic itself broke.
        resp = client.post("/jobs/ghost/cancel")
        assert resp.json().get("reason") == "not found"


# ──────────────────────────────────────────────────────────────────────
# Idempotency over HTTP
# ──────────────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_double_cancel_returns_200_both_times(self, client: TestClient) -> None:
        # The platform's cancel propagation is best-effort; a network
        # retry could fire the same cancel twice. The endpoint must
        # not 5xx on the second hit.
        job = job_manager.create(kind="test")
        first = client.post(f"/jobs/{job.job_id}/cancel")
        second = client.post(f"/jobs/{job.job_id}/cancel")
        assert first.status_code == 200
        assert second.status_code == 200

    def test_cancel_after_done_does_not_flip_status(self, client: TestClient) -> None:
        # Race: subprocess finished and runner wrote mark_done before
        # the platform's cancel arrived. The cancel should be a clean
        # no-op (200) and the done status must survive.
        job = job_manager.create(kind="test")
        job_manager.mark_done(job.job_id, {"plan": "real result"})
        resp = client.post(f"/jobs/{job.job_id}/cancel")
        assert resp.status_code == 200
        # Status response still shows done with the original result.
        status_resp = client.get(f"/jobs/{job.job_id}/status")
        assert status_resp.json()["status"] == "done"
        assert status_resp.json()["result"] == {"plan": "real result"}
