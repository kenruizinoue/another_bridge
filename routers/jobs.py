from typing import Any

import structlog
from fastapi import APIRouter, Request

from config import ANOTHER_CODER_RATE_LIMIT_JOBS
from jobs import job_manager
from services.rate_limiter import limiter

log = structlog.get_logger()

router = APIRouter()


@router.get("/jobs/{job_id}/status")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_JOBS)
def get_job_status_get(request: Request, job_id: str) -> dict[str, Any]:
    """Status endpoint. The platform's async webhook poller will hit this URL
    repeatedly until status != "running" or the platform-side timeout fires.

    Both GET and POST are accepted because the platform may call either
    depending on its dispatcher implementation."""
    job = job_manager.get(job_id)
    if job is None:
        return {"error": f"job not found: {job_id}", "status": "failed"}
    return job.to_status_response()


@router.post("/jobs/{job_id}/status")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_JOBS)
def get_job_status_post(request: Request, job_id: str) -> dict[str, Any]:
    return get_job_status_get(request, job_id)


@router.get("/jobs/{job_id}/chat/status")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_JOBS)
def get_chat_job_status(request: Request, job_id: str) -> dict[str, Any]:
    """Polling endpoint for chat_stream jobs — returns the live snapshot
    of accumulated assistant text + done flag. Used by the platform's
    bridge-status proxy when a mobile client's SSE drops mid-stream and
    needs to resume showing progress in the existing chat bubble.

    Distinct from /jobs/<id>/status (which is the generic job-progress
    endpoint async webhooks poll) because chat jobs surface different
    fields: the streamed text isn't in job.result (it lives in
    job.accumulated_text), and the polling client needs `done` rather
    than `status` to decide when to switch from polling to fetching the
    final saved message via /messages?since=.

    404-shaped response (status: "failed") when the job is unknown so
    the platform proxy can return a clean 404 to the frontend instead
    of a malformed shape that breaks the polling loop."""
    snapshot = job_manager.get_chat_status(job_id)
    if snapshot is None:
        return {"error": f"job not found: {job_id}", "status": "failed", "done": True}
    return snapshot


@router.post("/jobs/{job_id}/cancel")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_JOBS)
def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
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
