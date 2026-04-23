from typing import Any

import structlog
from fastapi import APIRouter, Request

log = structlog.get_logger()

router = APIRouter()


def _extract_args(body: Any) -> dict[str, Any]:
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}
    return args


@router.post("/tools/instruct_implementation")
async def instruct_implementation(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = _extract_args(body)
    ticket_number_raw = args.get("ticket_number")
    ticket_body = args.get("ticket_body")
    plan = args.get("plan")
    repo_path = args.get("repo_path")

    log.info(
        "instruct_implementation.request",
        ticket_number=ticket_number_raw,
        ticket_body_len=len(ticket_body) if isinstance(ticket_body, str) else None,
        plan_len=len(plan) if isinstance(plan, str) else None,
        repo_path=repo_path,
    )

    try:
        ticket_number = int(ticket_number_raw)
    except (TypeError, ValueError):
        log.error("instruct_implementation.invalid_ticket_number", value=ticket_number_raw)
        return {"error": "ticket_number is required and must be an integer"}

    if not isinstance(ticket_body, str) or not ticket_body.strip():
        log.error("instruct_implementation.missing_ticket_body")
        return {"error": "ticket_body is required and must be a non-empty string"}

    if not isinstance(plan, str) or not plan.strip():
        log.error("instruct_implementation.missing_plan")
        return {"error": "plan is required and must be a non-empty string"}

    branch_name = f"agent/ticket-{ticket_number}"

    log.info("instruct_implementation.stub_response", ticket_number=ticket_number, branch_name=branch_name)
    return {
        "pr_url": f"https://github.com/STUB/STUB/pull/STUB-{ticket_number}",
        "branch_name": branch_name,
        "ticket_number": ticket_number,
        "files_changed": [],
        "commits": 0,
        "duration_seconds": 0.0,
        "stub": True,
    }
