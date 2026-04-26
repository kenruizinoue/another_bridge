import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


JobStatus = Literal["running", "done", "failed"]


@dataclass
class Job:
    job_id: str
    kind: str
    status: JobStatus = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

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

    def mark_done(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "done"
            job.result = result
            job.finished_at = time.time()

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error
            job.finished_at = time.time()


# Module-level singleton — every router shares the same in-memory store.
job_manager = JobManager()
