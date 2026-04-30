import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog


JobStatus = Literal["running", "done", "failed"]

# Grace period between SIGTERM and SIGKILL when a cancel request comes
# in. Claude Code subprocesses usually exit cleanly on SIGTERM (they
# stream output and check signals between LLM rounds), so this gives
# them a beat before we hard-kill. Tunable; 3s is a balance between
# "give it time to flush" and "the user clicked cancel and wants it
# gone NOW".
SIGKILL_GRACE_SECONDS = 3.0

log = structlog.get_logger()


@dataclass
class Job:
    job_id: str
    kind: str
    status: JobStatus = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    # When True, an external cancel request fired. The runner that owns
    # this job should detect it after subprocess exit and mark_failed
    # with the cancel marker instead of mark_done with a (now stale)
    # result. Setting it does not by itself terminate the subprocess —
    # JobManager.cancel handles the SIGTERM/SIGKILL escalation.
    cancelled: bool = False
    # Held only while the underlying Claude Code subprocess is alive.
    # Reset to None once the process exits so JobManager.cancel on a
    # finished job is a clean no-op.
    process: subprocess.Popen | None = field(default=None, repr=False)
    # Live-accumulating text buffer for streaming chat jobs (kind="chat_stream").
    # Each text chunk extracted from Claude Code's stream-json output is
    # appended here so that GET /jobs/<id>/chat/status can return a snapshot
    # of "what's been generated so far" — used by the platform's polling
    # reconnect path when a mobile client's SSE drops mid-stream. Empty for
    # non-chat jobs (planning/implementation use job.result instead).
    accumulated_text: str = ""

    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 2)

    def to_status_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds(),
        }
        if self.status == "done" and self.result is not None:
            body["result"] = self.result
        if self.status == "failed" and self.error is not None:
            body["error"] = self.error
        return body


class JobManager:
    """In-memory job tracker. Lost on uvicorn restart — disk persistence is a
    follow-up ticket. Thread-safe because BackgroundTasks may run in a worker
    thread."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        job = Job(job_id=str(uuid.uuid4()), kind=kind)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def attach_process(self, job_id: str, proc: subprocess.Popen) -> None:
        """Register the running subprocess so an external cancel can
        signal it. If a cancel arrived BEFORE the process spawned (rare
        race; cancel mid-spawn), kill it immediately rather than letting
        the runner run a process the client already gave up on."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.process = proc
            already_cancelled = job.cancelled
        if already_cancelled:
            self._terminate_process(proc, job_id, reason="cancel-before-attach")

    def detach_process(self, job_id: str) -> None:
        """Drop the subprocess reference once it's exited. Keeps cancel()
        from operating on an already-reaped pid."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.process = None

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancelled)

    def append_text(self, job_id: str, chunk: str) -> None:
        """Append a streamed text chunk to the job's accumulated_text buffer.
        Thread-safe — chat_stream runs the SSE generator in a request worker
        thread while polling clients hit the status endpoint from other
        worker threads. No-op for unknown job_ids (graceful for races where
        a poll arrives just after the job was reaped). Empty chunks are
        skipped to avoid pointless lock churn."""
        if not chunk:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.accumulated_text += chunk

    def get_chat_status(self, job_id: str) -> dict[str, Any] | None:
        """Snapshot of a chat_stream job's progress for the polling reconnect
        path. Returns None for unknown job_ids so the caller can 404 cleanly.
        accumulated_text is whatever has been streamed so far — empty string
        is valid (job spawned, hasn't yet emitted any assistant text). The
        ``done`` boolean is the polling client's terminator: when True, the
        platform will switch from polling to fetching the saved final
        message via the normal /messages?since= path.

        Deliberately does NOT surface ``elapsed_seconds``: the platform's
        bridge-status proxy strips that field on the way to the frontend
        (the polling client measures wall-clock locally), so emitting it
        here is dead weight over ngrok. The generic ``to_status_response``
        used by /jobs/<id>/status keeps it because async-webhook callers
        do read it for trace bookkeeping."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "jobId": job.job_id,
                "status": job.status,
                "accumulatedText": job.accumulated_text,
                "done": job.status != "running",
            }

    def cancel(self, job_id: str) -> bool:
        """Mark the job cancelled and signal its subprocess to terminate.
        Returns True when a job with this id existed (and a kill was
        attempted if a process was attached). Returns False for unknown
        job_ids. Safe to call on already-finished jobs — no-op.

        Sends SIGTERM first; a background timer escalates to SIGKILL
        after SIGKILL_GRACE_SECONDS if the process hasn't exited. The
        runner detects job.cancelled after the process exits and writes
        a 'cancelled' failure status."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status != "running":
                # Already done / failed — nothing to kill, nothing to flag.
                return True
            job.cancelled = True
            proc = job.process

        if proc is None:
            # Cancel arrived before the subprocess was attached. The
            # cancel flag is set, so attach_process will kill it on
            # arrival. Common when cancel races with kickoff.
            log.info("job.cancel.no_process_yet", job_id=job_id)
            return True

        self._terminate_process(proc, job_id, reason="cancel")
        return True

    def _terminate_process(
        self, proc: subprocess.Popen, job_id: str, reason: str
    ) -> None:
        """Send SIGTERM, then SIGKILL after the grace period if needed.
        Wrapped in try/except since the process may have just exited on
        its own — that's a benign race, not an error."""
        try:
            if proc.poll() is None:
                # SIGTERM the process group so any descendants Claude
                # Code spawned (git, file tools, etc.) also stop. Falls
                # back to a single-process kill on platforms where
                # process groups aren't available.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    proc.terminate()
                log.info("job.cancel.sigterm_sent", job_id=job_id, pid=proc.pid, reason=reason)
        except Exception as err:
            log.warning("job.cancel.sigterm_failed", job_id=job_id, err=str(err))
            return

        # Escalate to SIGKILL after grace period if process is still
        # alive. Run in a daemon thread so we don't block the cancel
        # response.
        def _escalate() -> None:
            time.sleep(SIGKILL_GRACE_SECONDS)
            try:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        proc.kill()
                    log.warning("job.cancel.sigkill_sent", job_id=job_id, pid=proc.pid)
            except Exception as err:
                log.warning("job.cancel.sigkill_failed", job_id=job_id, err=str(err))

        threading.Thread(target=_escalate, daemon=True).start()

    def mark_done(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # Don't overwrite a cancellation. If cancel landed before
            # the runner finished its bookkeeping, the cancel must win
            # — the user already gave up on this result.
            if job.cancelled and job.status == "running":
                job.status = "failed"
                job.error = "cancelled by client"
                job.finished_at = time.time()
                job.process = None
                return
            job.status = "done"
            job.result = result
            job.finished_at = time.time()
            job.process = None

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            # Surface the cancel reason explicitly; otherwise use the
            # caller-supplied error verbatim.
            job.error = "cancelled by client" if job.cancelled else error
            job.finished_at = time.time()
            job.process = None


# Module-level singleton — every router shares the same in-memory store.
job_manager = JobManager()
