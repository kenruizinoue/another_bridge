import os
import subprocess
import time
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from config import CLAUDE_MODEL, CODING_REPO_PATH
from jobs import job_manager
from routers.repos import _git_origin_url, _parse_owner_repo, validate_repo_path


def _build_selected_repo_context(repo_path: str) -> dict[str, Any]:
    """Shape the platform's __context__.selected_repo payload from a
    resolved repo path. The platform persists this on the assistant
    message and surfaces it in the next turn's [Conversation context]
    block so the Developer Agent can read repo_path structurally instead
    of parsing markdown out of conversation history.

    owner_repo is best-effort — when the origin remote isn't a GitHub URL
    (or no origin is configured), the field is omitted. The path + name
    are always present.
    """
    out: dict[str, Any] = {
        "path": repo_path,
        "name": os.path.basename(repo_path.rstrip("/")),
    }
    origin_url = _git_origin_url(repo_path)
    if origin_url:
        owner_repo = _parse_owner_repo(origin_url)
        if owner_repo:
            out["owner_repo"] = owner_repo
    return out

log = structlog.get_logger()

router = APIRouter()

# 30 minutes — matches the platform's per-tool pollMaxSeconds (1800s) on
# the async-webhook EM template, so the platform's poll budget and this
# subprocess budget end together. If we cap shorter, the platform thinks
# it has more time than we do, and a slightly long planning gets killed
# here before the platform sees a result.
PLANNING_TIMEOUT_SECONDS = 1800


def _extract_args(body: Any) -> dict[str, Any]:
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}
    return args


def _run_planning_job(
    job_id: str,
    ticket_number: int,
    ticket_body: str,
    resolved_repo_path: str,
) -> None:
    """Runs the actual Claude planning subprocess. Called in a BackgroundTask
    so the HTTP response returns immediately with the job_id."""
    workspace_dir = os.path.dirname(resolved_repo_path.rstrip("/"))

    prompt = (
        f"You are planning work inside the repo located at {resolved_repo_path}. "
        f"Sibling repos live under {workspace_dir} — e.g. \"../<sibling_repo_name>\" "
        f"is reachable from here. You may READ files in sibling repos when the ticket "
        f"references them (use file tools), but do NOT modify anything outside the "
        f"target repo. Your edits (in a later step) will be scoped to the target repo only.\n\n"
        f"Read the ticket below and produce a numbered implementation plan.\n\n"
        f"Requirements for the plan:\n"
        f"- Reference concrete file paths (relative to the repo root) that you would touch.\n"
        f"- Be concise: keep the whole plan under ~2000 tokens.\n"
        f"- Prefer 5-10 numbered steps; each step is one sentence or a short paragraph.\n"
        f"- Do NOT modify any files — this is a plan only.\n"
        f"- Do NOT include steps that start dev servers (npm run dev, npm start, uvicorn,\n"
        f"  yarn dev, etc.) or that depend on a running backend. The implementation runs\n"
        f"  headless without local services. If verification is needed, the plan can\n"
        f"  reference type-checks or unit tests, but never long-running processes.\n\n"
        f"Ticket #{ticket_number}:\n{ticket_body.strip()}"
    )

    log.info(
        "instruct_planning.running_claude",
        job_id=job_id,
        ticket_number=ticket_number,
        model=CLAUDE_MODEL,
        repo_path=resolved_repo_path,
        workspace_dir=workspace_dir,
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
            job_id=job_id,
            ticket_number=ticket_number,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, f"timeout after {duration}s")
        return

    duration = round(time.time() - started, 2)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log.error(
            "instruct_planning.claude_failed",
            job_id=job_id,
            ticket_number=ticket_number,
            exit_code=result.returncode,
            stderr=stderr,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, stderr or f"claude exited with code {result.returncode}")
        return

    plan = (result.stdout or "").strip()
    log.info(
        "instruct_planning.ok",
        job_id=job_id,
        ticket_number=ticket_number,
        plan_len=len(plan),
        duration_seconds=duration,
    )
    job_manager.mark_done(
        job_id,
        {
            "plan": plan,
            "ticket_number": ticket_number,
            "repo_path": resolved_repo_path,
            "exit_code": 0,
            "duration_seconds": duration,
            # The platform strips __context__ from the LLM-visible response
            # body and persists each key as a context artifact on the
            # assistant message. Next turn's prompt injects them as
            # [Conversation context] so the Developer Agent reads
            # selected_repo.path directly instead of parsing markdown.
            "__context__": {
                "selected_repo": _build_selected_repo_context(resolved_repo_path),
            },
        },
    )


@router.post("/tools/instruct_planning")
async def instruct_planning(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
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
        return {"error": "ticket_number is required and must be an integer"}

    if not isinstance(ticket_body, str) or not ticket_body.strip():
        return {"error": "ticket_body is required and must be a non-empty string"}

    resolved_repo_path = repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    resolved_repo_path, err = validate_repo_path(resolved_repo_path)
    if err:
        return {"error": err}

    job = job_manager.create(kind="instruct_planning")
    background_tasks.add_task(
        _run_planning_job,
        job.job_id,
        ticket_number,
        ticket_body,
        resolved_repo_path,
    )

    log.info("instruct_planning.dispatched", job_id=job.job_id, ticket_number=ticket_number)
    return {
        "job_id": job.job_id,
        "kind": "instruct_planning",
        "status": "running",
        "status_url": f"/jobs/{job.job_id}/status",
    }
