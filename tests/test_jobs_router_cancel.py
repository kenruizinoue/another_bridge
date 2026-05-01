"""Tests for POST /jobs/<id>/cancel route handler.

The endpoint is a thin wrapper around JobManager.cancel — these
tests lock down the response shape (job_id + cancelled flag) so
the platform's cancel-propagation expectation can't be silently
broken by a future refactor.

Goes through FastAPI's TestClient rather than calling the handler
function directly because the rate-limit decorator now requires a
real Request — matches the convention in the rest of the chat /
jobs test files.
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


class TestCancelEndpoint:
    def test_cancel_unknown_job_returns_cancelled_false(
        self, client: TestClient
    ) -> None:
        resp = client.post("/jobs/no-such-id/cancel")
        assert resp.status_code == 200
        assert resp.json() == {
            "job_id": "no-such-id",
            "cancelled": False,
            "reason": "not found",
        }

    def test_cancel_running_job_returns_cancelled_true(
        self, client: TestClient
    ) -> None:
        job = job_manager.create(kind="test")
        try:
            resp = client.post(f"/jobs/{job.job_id}/cancel")
            assert resp.status_code == 200
            assert resp.json() == {"job_id": job.job_id, "cancelled": True}
            # Side effect: the job is now flagged cancelled.
            assert job_manager.is_cancelled(job.job_id) is True
        finally:
            job_manager._jobs.pop(job.job_id, None)  # type: ignore[attr-defined]

    def test_cancel_already_finished_job_is_idempotent_success(
        self, client: TestClient
    ) -> None:
        job = job_manager.create(kind="test")
        try:
            job_manager.mark_done(job.job_id, {"ok": True})
            resp = client.post(f"/jobs/{job.job_id}/cancel")
            assert resp.status_code == 200
            assert resp.json() == {"job_id": job.job_id, "cancelled": True}
            # Status remains done; cancel did not flip it.
            assert job_manager.get(job.job_id).status == "done"  # type: ignore[union-attr]
        finally:
            job_manager._jobs.pop(job.job_id, None)  # type: ignore[attr-defined]
