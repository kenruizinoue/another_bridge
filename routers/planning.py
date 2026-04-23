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


@router.post("/tools/instruct_planning")
async def instruct_planning(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = _extract_args(body)
    ticket_number_raw = args.get("ticket_number")
    ticket_body = args.get("ticket_body")
    repo_path = args.get("repo_path")

    log.info(
        "instruct_planning.request",
        ticket_number=ticket_number_raw,
        ticket_body_len=len(ticket_body) if isinstance(ticket_body, str) else None,
        repo_path=repo_path,
    )

    try:
        ticket_number = int(ticket_number_raw)
    except (TypeError, ValueError):
        log.error("instruct_planning.invalid_ticket_number", value=ticket_number_raw)
        return {"error": "ticket_number is required and must be an integer"}

    if not isinstance(ticket_body, str) or not ticket_body.strip():
        log.error("instruct_planning.missing_ticket_body")
        return {"error": "ticket_body is required and must be a non-empty string"}

    log.info("instruct_planning.stub_response", ticket_number=ticket_number)
    return {
        "plan": f"STUB PLAN: implement ticket {ticket_number}",
        "ticket_number": ticket_number,
    }
