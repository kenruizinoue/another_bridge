"""Coverage for ``services.claude_runner.run_blocking``.

This is the synchronous spawn seam used by both
``/tools/instruct_planning`` and ``/tools/instruct_implementation``.
Every webhook ticket the agent processes flows through here. The
function classifies its outcome into four discriminated states
(spawn_error / timed_out / cancelled / clean) that downstream
routers map to ``error_kind`` on /jobs/<id>/status — the platform
branches its UI on those kinds, so the classification matters
end-to-end.

We mock ``subprocess.Popen`` at the same seam every other test in
this suite uses (``claude_runner.subprocess.Popen``) and exercise
the four branches directly. Each test uses a real ``JobManager``
job created via the fixture so attach/detach + is_cancelled wire
through their actual code paths instead of being mocked.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from jobs import job_manager
from services import claude_runner


@pytest.fixture
def job_id() -> str:
    """Real JobManager job — needed because run_blocking calls
    attach_process + is_cancelled, which return early on unknown ids
    and would silently bypass the cancellation branch."""
    job = job_manager.create("instruct_planning")
    return job.job_id


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Mock Popen result — communicate(timeout=...) returns
    (stdout, stderr); returncode reads off the attribute."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 4242
    return proc


class TestRunBlocking:
    def test_happy_path_returns_clean_result(self, job_id: str) -> None:
        # Clean run: rc=0, stdout populated, no spawn_error, no
        # timed_out, no cancelled. This is the path planning +
        # implementation rely on for "claude returned a plan".
        proc = _proc(returncode=0, stdout="here is the plan")
        with patch.object(claude_runner.subprocess, "Popen", return_value=proc):
            result = claude_runner.run_blocking(
                args=["claude", "-p", "plan ticket"],
                cwd="/tmp",
                timeout_seconds=60,
                job_id=job_id,
            )
        assert result.returncode == 0
        assert result.stdout == "here is the plan"
        assert result.spawn_error is None
        assert result.timed_out is False
        assert result.cancelled is False

    def test_spawn_error_when_binary_missing(self, job_id: str) -> None:
        # FileNotFoundError at Popen → ClaudeResult with spawn_error
        # populated and returncode=-1. Maps to error_kind="spawn_failed"
        # downstream. This is the day-1 NVM-PATH bug the operator hits.
        with patch.object(
            claude_runner.subprocess,
            "Popen",
            side_effect=FileNotFoundError("claude not on PATH"),
        ):
            result = claude_runner.run_blocking(
                args=["claude", "-p", "anything"],
                cwd="/tmp",
                timeout_seconds=60,
                job_id=job_id,
            )
        assert result.spawn_error is not None
        assert "claude not on PATH" in result.spawn_error
        assert result.returncode == -1
        assert result.timed_out is False

    def test_timeout_kills_process_and_flags_timed_out(
        self, job_id: str
    ) -> None:
        # First communicate() raises TimeoutExpired → run_blocking calls
        # proc.kill() and re-communicate()'s. ClaudeResult.timed_out=True
        # is what maps to error_kind="timeout" in the planning/
        # implementation handlers. Without proc.kill() the subprocess
        # leaks and the worker thread hangs forever.
        proc = MagicMock(spec=subprocess.Popen)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=60),
            ("partial stdout", "partial stderr"),
        ]
        proc.returncode = -9
        proc.pid = 4242

        with patch.object(claude_runner.subprocess, "Popen", return_value=proc):
            result = claude_runner.run_blocking(
                args=["claude", "-p", "long task"],
                cwd="/tmp",
                timeout_seconds=60,
                job_id=job_id,
            )
        assert result.timed_out is True
        proc.kill.assert_called_once()
        # Second communicate after kill produced final stdout/stderr.
        assert result.stdout == "partial stdout"

    def test_external_cancel_surfaces_on_result(self, job_id: str) -> None:
        # Cancel arrives mid-run (e.g. user clicks cancel from the
        # platform UI). We simulate by flipping the job's cancelled
        # flag before run_blocking checks is_cancelled at the end.
        # Result.cancelled=True is how the runner detects this and
        # downstream maps it to error_kind="cancelled" instead of
        # treating a clean-rc=0-but-cancelled outcome as a success.
        with job_manager._lock:  # type: ignore[attr-defined]
            job_manager._jobs[job_id].cancelled = True  # type: ignore[attr-defined]

        proc = _proc(returncode=0, stdout="result-the-user-no-longer-wants")
        with patch.object(claude_runner.subprocess, "Popen", return_value=proc):
            result = claude_runner.run_blocking(
                args=["claude", "-p", "anything"],
                cwd="/tmp",
                timeout_seconds=60,
                job_id=job_id,
            )
        assert result.cancelled is True
