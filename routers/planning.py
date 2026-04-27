import os
import re
import subprocess
import time
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from config import CLAUDE_MODEL, CODING_REPO_PATH
from jobs import job_manager
from routers.repos import _git_origin_url, _parse_owner_repo, validate_repo_path


# Matches a trailing `SUMMARY: <text>` line in Claude's output. The plan
# is everything before it; the summary feeds the platform's __summary__
# opt-in (consumed by extractArtifactCandidates → artifact.summary, then
# rendered in the next-turn [Conversation artifacts] block).
_SUMMARY_LINE_RE = re.compile(r"\n\s*SUMMARY:\s*(.+?)\s*$", re.DOTALL)


def split_plan_and_summary(claude_output: str) -> tuple[str, str | None]:
    """Split Claude's planning output into (plan, summary).

    Claude is instructed to end with `SUMMARY: <one sentence>`. When that
    line is present, return (plan_without_it, summary). When absent
    (Claude ignored the instruction or the output was truncated), return
    (claude_output, None) — the platform's auto-summary fallback then
    handles it. Pure function, no I/O — easy to unit-test.
    """
    match = _SUMMARY_LINE_RE.search(claude_output)
    if not match:
        return claude_output.strip(), None
    summary = match.group(1).strip()
    plan = claude_output[: match.start()].strip()
    if not summary:
        # Edge case: `SUMMARY:` line present but empty after the colon.
        # Treat as missing so the platform falls back to auto-summary.
        return plan, None
    return plan, summary


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
        f"- Prefer 5-10 numbered steps. Each step MUST be a self-contained,\n"
        f"  COMMITTABLE unit of work — the implementer will create one git\n"
        f"  commit per numbered step (in plan order) so the branch's commit\n"
        f"  history mirrors the plan exactly. Design accordingly:\n"
        f"     - A step should leave the repo in a coherent state when\n"
        f"       committed alone (no half-applied refactors split across\n"
        f"       two steps; no \"add file in step 3, populate it in step 5\").\n"
        f"     - If two changes only make sense together (e.g. add a function\n"
        f"       and its caller in the same edit), keep them in ONE step,\n"
        f"       not two.\n"
        f"     - Order steps so each commit builds on the previous one\n"
        f"       cleanly — earliest commits set up scaffolding, later ones\n"
        f"       extend or wire it up.\n"
        f"     - Begin each step with a short imperative phrase that doubles\n"
        f"       as the commit subject (e.g. \"Add UserSchema with email\n"
        f"       validation\" — fine as a commit message).\n"
        f"- Do NOT modify any files — this is a plan only.\n"
        f"- Do NOT include steps that start dev servers (npm run dev, npm start, uvicorn,\n"
        f"  yarn dev, etc.) or that depend on a running backend. The implementation runs\n"
        f"  headless without local services. If verification is needed, the plan can\n"
        f"  reference type-checks or unit tests, but never long-running processes.\n\n"
        f"After the plan, on its own line at the very end, output exactly one line:\n"
        f"  SUMMARY: <one sentence, max 200 characters, naming what this plan changes\n"
        f"  and which areas/files it touches — written so the next agent can decide\n"
        f"  at a glance whether to load the full plan>\n"
        f"This SUMMARY line is the ONLY thing after the plan. No explanation, no\n"
        f"trailing prose. Example:\n"
        f"  SUMMARY: Ticket #34 plan — extracts session-token validation into shared\n"
        f"  middleware (src/auth/) and adds 3 unit tests.\n\n"
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

    # Popen (not subprocess.run) so we can attach the process to the job
    # and let an external cancel send SIGTERM. start_new_session puts
    # claude in its own process group so killpg also reaches any
    # descendants Claude Code spawns (git, file tools, mcp servers).
    try:
        proc = subprocess.Popen(
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=resolved_repo_path,
            start_new_session=True,
        )
    except FileNotFoundError as err:
        log.error("instruct_planning.spawn_failed", job_id=job_id, err=str(err))
        job_manager.mark_failed(job_id, f"failed to spawn claude: {err}")
        return

    job_manager.attach_process(job_id, proc)

    try:
        stdout, stderr = proc.communicate(timeout=PLANNING_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        duration = round(time.time() - started, 2)
        log.error(
            "instruct_planning.timeout",
            job_id=job_id,
            ticket_number=ticket_number,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, f"timeout after {duration}s")
        return
    finally:
        job_manager.detach_process(job_id)

    duration = round(time.time() - started, 2)

    # Cancel arrived during the run → SIGTERM/SIGKILL killed Claude;
    # whatever stdout it produced is incomplete and not worth surfacing.
    if job_manager.is_cancelled(job_id):
        log.info(
            "instruct_planning.cancelled",
            job_id=job_id,
            ticket_number=ticket_number,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, "cancelled by client")
        return

    if proc.returncode != 0:
        stderr_text = (stderr or "").strip()
        log.error(
            "instruct_planning.claude_failed",
            job_id=job_id,
            ticket_number=ticket_number,
            exit_code=proc.returncode,
            stderr=stderr_text,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, stderr_text or f"claude exited with code {proc.returncode}")
        return

    raw_output = (stdout or "").strip()
    plan, summary = split_plan_and_summary(raw_output)
    log.info(
        "instruct_planning.ok",
        job_id=job_id,
        ticket_number=ticket_number,
        plan_len=len(plan),
        summary_present=summary is not None,
        duration_seconds=duration,
    )
    payload: dict[str, Any] = {
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
    }
    # The platform strips __summary__ from the LLM-visible response body
    # and stashes it as the artifact's `summary` field. Shown in every
    # subsequent turn's [Conversation artifacts] block, so the Developer
    # Agent can decide at a glance whether to fetch_artifact for the
    # full plan. Without this, the platform falls back to slicing the
    # first 200 chars of the plan field — usable, but mechanical.
    if summary:
        payload["__summary__"] = summary
    job_manager.mark_done(job_id, payload)


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
        # Hint the recovery path so the LLM (Planner Agent) can self-correct
        # on its next pass without needing a prompt change. If the upstream
        # github_get_issue returned an empty body, the agent should
        # synthesize ticket_body from the issue title + the user's chat
        # context, not bail.
        return {
            "error": (
                "ticket_body is required and must be a non-empty string. "
                "If the GitHub issue body is empty, build ticket_body from "
                "the issue title plus the user's description in the chat "
                "(do not pass empty)."
            )
        }

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
        # The platform fires this best-effort when the user cancels the
        # chat turn so the Claude Code subprocess actually stops instead
        # of running to completion with its result silently dropped.
        "cancel_url": f"/jobs/{job.job_id}/cancel",
    }
