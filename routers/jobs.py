from typing import Any

import structlog
from fastapi import APIRouter

from jobs import job_manager

log = structlog.get_logger()

router = APIRouter()


@router.get("/jobs/{job_id}/status")
def get_job_status_get(job_id: str) -> dict[str, Any]:
    """Status endpoint. The platform's async webhook poller will hit this URL
    repeatedly until status != "running" or the platform-side timeout fires.

    Both GET and POST are accepted because the platform may call either
    depending on its dispatcher implementation."""
    job = job_manager.get(job_id)
    if job is None:
        return {"error": f"job not found: {job_id}", "status": "failed"}
    return job.to_status_response()


@router.post("/jobs/{job_id}/status")
def get_job_status_post(job_id: str) -> dict[str, Any]:
    return get_job_status_get(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job. Triggered by the platform's executeTool
    dispatcher when the user cancels a chat turn — the platform aborts
    its polling fetch AND fires this endpoint so the Claude Code
    subprocess running here also stops, instead of burning tokens until
    its result is silently dropped.

    Idempotent: cancelling an already-finished or unknown job is a
    success no-op (returns cancelled=true / cancelled=false respectively
    but never errors). The platform's catch block fires this best-effort
    and shouldn't fail the chat cancel if the coder is offline."""
    ok = job_manager.cancel(job_id)
    if not ok:
        log.info("job.cancel.not_found", job_id=job_id)
        return {"job_id": job_id, "cancelled": False, "reason": "not found"}
    log.info("job.cancel.requested", job_id=job_id)
    return {"job_id": job_id, "cancelled": True}
