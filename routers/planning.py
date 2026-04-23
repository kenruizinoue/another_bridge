import os
import subprocess
import time
from typing import Any

import structlog
from fastapi import APIRouter, Request

from config import CLAUDE_MODEL, CODING_REPO_PATH

log = structlog.get_logger()

router = APIRouter()

PLANNING_TIMEOUT_SECONDS = 900


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

    resolved_repo_path = repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        log.error("instruct_planning.missing_repo_path")
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    if not os.path.isdir(resolved_repo_path):
        log.error("instruct_planning.repo_path_not_found", repo_path=resolved_repo_path)
        return {"error": f"repo_path does not exist or is not a directory: {resolved_repo_path}"}

    prompt = (
        f"You are planning work inside the repo located at {resolved_repo_path}. "
        f"Read the ticket below and produce a numbered implementation plan.\n\n"
        f"Requirements for the plan:\n"
        f"- Reference concrete file paths (relative to the repo root) that you would touch.\n"
        f"- Be concise: keep the whole plan under ~2000 tokens.\n"
        f"- Prefer 5-10 numbered steps; each step is one sentence or a short paragraph.\n"
        f"- Do NOT modify any files — this is a plan only.\n\n"
        f"Ticket #{ticket_number}:\n{ticket_body.strip()}"
    )

    log.info(
        "instruct_planning.running_claude",
        ticket_number=ticket_number,
        model=CLAUDE_MODEL,
        repo_path=resolved_repo_path,
    )
    started = time.time()

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--model",
                CLAUDE_MODEL,
                "--output-format",
                "text",
                "--dangerously-skip-permissions",
            ],
            capture_output=True,
            text=True,
            timeout=PLANNING_TIMEOUT_SECONDS,
            cwd=resolved_repo_path,
        )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - started, 2)
        log.error(
            "instruct_planning.timeout",
            ticket_number=ticket_number,
            duration_seconds=duration,
            repo_path=resolved_repo_path,
        )
        return {"error": "timeout", "duration_seconds": duration, "repo_path": resolved_repo_path}

    duration = round(time.time() - started, 2)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log.error(
            "instruct_planning.claude_failed",
            ticket_number=ticket_number,
            exit_code=result.returncode,
            stderr=stderr,
            duration_seconds=duration,
            repo_path=resolved_repo_path,
        )
        return {
            "error": stderr or "claude exited non-zero",
            "exit_code": result.returncode,
            "duration_seconds": duration,
            "repo_path": resolved_repo_path,
        }

    plan = (result.stdout or "").strip()
    log.info(
        "instruct_planning.ok",
        ticket_number=ticket_number,
        plan_len=len(plan),
        duration_seconds=duration,
        repo_path=resolved_repo_path,
    )
    return {
        "plan": plan,
        "ticket_number": ticket_number,
        "repo_path": resolved_repo_path,
        "exit_code": 0,
        "duration_seconds": duration,
    }
