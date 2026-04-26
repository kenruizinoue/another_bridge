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
