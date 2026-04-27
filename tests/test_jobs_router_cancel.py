"""Tests for POST /jobs/<id>/cancel route handler.

The endpoint is a thin wrapper around JobManager.cancel — these tests
lock down the response shape (job_id + cancelled flag) so the platform's
cancel-propagation expectation can't be silently broken by a future
refactor. Calls the handler function directly rather than going through
FastAPI/TestClient (matches the in-repo convention — see
test_implementation_pr_creation.py)."""

from jobs import job_manager
from routers.jobs import cancel_job


class TestCancelEndpoint:
    def test_cancel_unknown_job_returns_cancelled_false(self) -> None:
        body = cancel_job("no-such-id")
        assert body == {
            "job_id": "no-such-id",
            "cancelled": False,
            "reason": "not found",
        }

    def test_cancel_running_job_returns_cancelled_true(self) -> None:
        job = job_manager.create(kind="test")
        try:
            body = cancel_job(job.job_id)
            assert body == {"job_id": job.job_id, "cancelled": True}
            # Side effect: the job is now flagged cancelled.
            assert job_manager.is_cancelled(job.job_id) is True
        finally:
            job_manager._jobs.pop(job.job_id, None)  # type: ignore[attr-defined]

    def test_cancel_already_finished_job_is_idempotent_success(self) -> None:
        job = job_manager.create(kind="test")
        try:
            job_manager.mark_done(job.job_id, {"ok": True})
            body = cancel_job(job.job_id)
            assert body == {"job_id": job.job_id, "cancelled": True}
            # Status remains done; cancel did not flip it.
            assert job_manager.get(job.job_id).status == "done"
        finally:
            job_manager._jobs.pop(job.job_id, None)  # type: ignore[attr-defined]
