"""Tests for the cancel propagation in jobs.JobManager.

When the user cancels a chat turn in the platform, the platform-side
executeTool dispatcher aborts its polling fetch AND fires a best-effort
POST to the coder's /jobs/<id>/cancel endpoint. That hits
JobManager.cancel, which:

  1. Marks the job as cancelled.
  2. SIGTERMs the attached subprocess (process group, so descendants
     also stop).
  3. Schedules a SIGKILL escalation in a daemon thread after the grace
     period if the process hasn't exited.
  4. Lets the runner detect cancelled-ness after subprocess exit and
     write a 'cancelled' failure status.

These tests use a tiny stub Popen so we don't depend on `claude` being
on PATH and so the tests are deterministic and fast.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import jobs as jobs_module
from jobs import JobManager


# ──────────────────────────────────────────────────────────────────────
# Stub Popen — observable kill / poll behavior
# ──────────────────────────────────────────────────────────────────────


class _StubPopen:
    """Minimal stand-in for subprocess.Popen.
    poll() returns None until the test marks it dead.
    terminate/kill flip the dead flag, mimicking SIGTERM / SIGKILL."""

    def __init__(self, pid: int = 99999, dead: bool = False) -> None:
        self.pid = pid
        self._dead = dead
        self.terminate_called = 0
        self.kill_called = 0

    def poll(self) -> int | None:
        return 0 if self._dead else None

    def terminate(self) -> None:
        self.terminate_called += 1
        self._dead = True

    def kill(self) -> None:
        self.kill_called += 1
        self._dead = True


@pytest.fixture(autouse=True)
def _short_grace_period(monkeypatch: pytest.MonkeyPatch):
    """Shrink the SIGKILL grace so escalation tests don't take 3s."""
    monkeypatch.setattr(jobs_module, "SIGKILL_GRACE_SECONDS", 0.05)


@pytest.fixture
def mgr() -> JobManager:
    return JobManager()


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestJobLifecycle:
    def test_create_returns_running_job(self, mgr: JobManager) -> None:
        job = mgr.create(kind="instruct_planning")
        assert job.status == "running"
        assert job.cancelled is False
        assert job.process is None

    def test_attach_then_detach_clears_process_handle(self, mgr: JobManager) -> None:
        job = mgr.create(kind="instruct_planning")
        proc = _StubPopen()
        mgr.attach_process(job.job_id, proc)
        assert mgr.get(job.job_id).process is proc
        mgr.detach_process(job.job_id)
        assert mgr.get(job.job_id).process is None


# ──────────────────────────────────────────────────────────────────────
# Cancel — basic
# ──────────────────────────────────────────────────────────────────────


class TestCancelBasics:
    def test_cancel_unknown_job_returns_false(self, mgr: JobManager) -> None:
        assert mgr.cancel("does-not-exist") is False

    def test_cancel_already_finished_job_is_idempotent_noop(self, mgr: JobManager) -> None:
        # mark_done before cancel — cancel should not flip status back.
        job = mgr.create(kind="instruct_planning")
        mgr.mark_done(job.job_id, {"plan": "x"})
        assert mgr.cancel(job.job_id) is True  # job exists → True
        # status remains done (cancel ignored on already-finished jobs)
        assert mgr.get(job.job_id).status == "done"
        assert mgr.get(job.job_id).cancelled is False

    def test_cancel_sets_cancelled_flag(self, mgr: JobManager) -> None:
        job = mgr.create(kind="instruct_planning")
        # No process attached yet — cancel still flags.
        ok = mgr.cancel(job.job_id)
        assert ok is True
        assert mgr.is_cancelled(job.job_id) is True


# ──────────────────────────────────────────────────────────────────────
# Cancel — process signal escalation
# ──────────────────────────────────────────────────────────────────────


class TestCancelTerminatesProcess:
    def test_cancel_with_attached_running_process_sends_sigterm(
        self, mgr: JobManager
    ) -> None:
        job = mgr.create(kind="instruct_planning")
        proc = _StubPopen()
        mgr.attach_process(job.job_id, proc)

        # killpg may raise on this test process — we don't care about
        # the syscall path, only that proc.terminate() was reached as a
        # fallback. Patch killpg to always raise so we hit the fallback.
        with patch("jobs.os.killpg", side_effect=ProcessLookupError):
            assert mgr.cancel(job.job_id) is True

        assert proc.terminate_called == 1

    def test_cancel_escalates_to_sigkill_when_process_doesnt_exit(
        self, mgr: JobManager
    ) -> None:
        job = mgr.create(kind="instruct_planning")
        # Process refuses to die on terminate(). Override the test stub:
        proc = _StubPopen()

        def _stubborn_terminate() -> None:
            # Bump counter but DON'T flip dead — simulates a process
            # that ignores SIGTERM.
            proc.terminate_called += 1

        proc.terminate = _stubborn_terminate  # type: ignore[method-assign]

        mgr.attach_process(job.job_id, proc)
        with patch("jobs.os.killpg", side_effect=ProcessLookupError):
            mgr.cancel(job.job_id)

        # Grace period is 0.05s (autouse fixture). Wait a beat for the
        # escalation thread.
        time.sleep(0.15)
        assert proc.kill_called >= 1

    def test_no_sigkill_when_process_exited_within_grace(
        self, mgr: JobManager
    ) -> None:
        job = mgr.create(kind="instruct_planning")
        proc = _StubPopen()  # default dies on terminate()
        mgr.attach_process(job.job_id, proc)
        with patch("jobs.os.killpg", side_effect=ProcessLookupError):
            mgr.cancel(job.job_id)

        time.sleep(0.15)  # past the 0.05s grace window
        # proc died inside terminate(); escalation thread should see
        # poll() != None and NOT escalate.
        assert proc.kill_called == 0


# ──────────────────────────────────────────────────────────────────────
# Race: cancel arrives BEFORE attach_process
# ──────────────────────────────────────────────────────────────────────


class TestCancelBeforeAttach:
    def test_cancel_then_attach_kills_immediately(self, mgr: JobManager) -> None:
        # Order: create → cancel → attach. The runner spawned the
        # process AFTER the cancel — attach_process should detect the
        # cancelled flag and SIGTERM the new process right away.
        job = mgr.create(kind="instruct_planning")
        mgr.cancel(job.job_id)  # no process yet — flag only

        proc = _StubPopen()
        with patch("jobs.os.killpg", side_effect=ProcessLookupError):
            mgr.attach_process(job.job_id, proc)

        assert proc.terminate_called == 1


# ──────────────────────────────────────────────────────────────────────
# mark_done / mark_failed honor the cancel flag
# ──────────────────────────────────────────────────────────────────────


class TestMarkDoneRespectsCancel:
    def test_mark_done_after_cancel_writes_failed_with_cancel_marker(
        self, mgr: JobManager
    ) -> None:
        # Race scenario: subprocess finished and runner is calling
        # mark_done with a (now-stale) result, but cancel landed in the
        # interim. The cancel must win.
        job = mgr.create(kind="instruct_planning")
        mgr.cancel(job.job_id)
        mgr.mark_done(job.job_id, {"plan": "stale result, user gave up"})

        final = mgr.get(job.job_id)
        assert final.status == "failed"
        assert final.error == "cancelled by client"
        # No stale result leaks into the response.
        assert final.result is None

    def test_mark_failed_after_cancel_overrides_error_message(
        self, mgr: JobManager
    ) -> None:
        # Runner saw subprocess return non-zero (because we killed it)
        # and is calling mark_failed("claude exited with code 143").
        # That's noise — surface "cancelled by client" instead.
        job = mgr.create(kind="instruct_planning")
        mgr.cancel(job.job_id)
        mgr.mark_failed(job.job_id, "claude exited with code 143")

        final = mgr.get(job.job_id)
        assert final.status == "failed"
        assert final.error == "cancelled by client"

    def test_mark_done_without_cancel_writes_result_normally(
        self, mgr: JobManager
    ) -> None:
        # Sanity check: the cancel logic must not break the happy path.
        job = mgr.create(kind="instruct_planning")
        mgr.mark_done(job.job_id, {"plan": "real result"})

        final = mgr.get(job.job_id)
        assert final.status == "done"
        assert final.result == {"plan": "real result"}
        assert final.error is None


# ──────────────────────────────────────────────────────────────────────
# Status response includes elapsed_seconds and friends as before
# ──────────────────────────────────────────────────────────────────────


class TestStatusResponse:
    def test_status_response_after_cancel_failure_surfaces_marker(
        self, mgr: JobManager
    ) -> None:
        job = mgr.create(kind="instruct_planning")
        mgr.cancel(job.job_id)
        mgr.mark_failed(job.job_id, "anything")

        body = mgr.get(job.job_id).to_status_response()
        assert body["status"] == "failed"
        assert body["error"] == "cancelled by client"
        assert "result" not in body
