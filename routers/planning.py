import os
import re
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import ValidationError

from config import ANOTHER_CODER_RATE_LIMIT_INSTRUCT, CLAUDE_MODEL, CODING_REPO_PATH
from jobs import job_manager
from routers._schemas import PlanningRequest, first_error_message
from routers.repos import validate_repo_path
from services import claude_runner
from services.errors import CANCELLED, CLAUDE_FAILED, SPAWN_FAILED, TIMEOUT
from services.rate_limiter import limiter
from services.repo_context import build_selected_repo_context
from services.request import extract_args

# Tests patch routers.planning._build_selected_repo_context (autouse fixture
# in test_planning_payload). Keep the underscore-prefixed alias so the
# patch target stays valid without forcing test rewrites.
_build_selected_repo_context = build_selected_repo_context


# Matches a trailing `SUMMARY: <text>` line in Claude's output. The plan
# is everything before it; the summary feeds the platform's __summary__
# opt-in (consumed by extractArtifactCandidates → artifact.summary, then
# rendered in the next-turn [Conversation artifacts] block).
_SUMMARY_LINE_RE = re.compile(r"\n\s*SUMMARY:\s*(.+?)\s*$", re.DOTALL)

# Matches `TARGET_REPO_MISMATCH: true` anywhere on its own line. When
# Claude Code determines the ticket was filed against the wrong repo
# (per TARGET-REPO DISCIPLINE in the planning prompt), it appends this
# marker so the platform knows to SUPPRESS the selected_repo context
# emission. Without that suppression the wrong-repo run silently
# overwrites the prior turn's correct selected_repo, and the Planner
# then defaults to the wrong repo on subsequent refinement turns —
# cascade error.
_TARGET_REPO_MISMATCH_RE = re.compile(
    r"^\s*TARGET_REPO_MISMATCH:\s*true\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def detect_target_repo_mismatch(claude_output: str) -> bool:
    """Returns True when Claude Code flagged this run as targeting the
    wrong repo. Pure helper for testability."""
    return bool(_TARGET_REPO_MISMATCH_RE.search(claude_output))


def strip_target_repo_mismatch_marker(claude_output: str) -> str:
    """Removes the marker line from the visible plan text so the LLM
    consumer doesn't see the platform-internal control flag. Idempotent
    — returns the input unchanged when the marker is absent."""
    return _TARGET_REPO_MISMATCH_RE.sub("", claude_output).rstrip()


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


log = structlog.get_logger()

router = APIRouter()

# 30 minutes — matches the platform's per-tool pollMaxSeconds (1800s) on
# the async-webhook EM template, so the platform's poll budget and this
# subprocess budget end together. If we cap shorter, the platform thinks
# it has more time than we do, and a slightly long planning gets killed
# here before the platform sees a result.
PLANNING_TIMEOUT_SECONDS = 1800


def _build_planning_prompt(
    ticket_number: int,
    ticket_body: str,
    resolved_repo_path: str,
    workspace_dir: str,
) -> str:
    return (
        f"You are planning work inside the repo located at {resolved_repo_path}. "
        f"Sibling repos live under {workspace_dir} — e.g. \"../<sibling_repo_name>\" "
        f"is reachable from here. You may READ files in sibling repos when the ticket "
        f"references them (use file tools), but do NOT modify anything outside the "
        f"target repo. Your edits (in a later step) will be scoped to the target repo only.\n\n"
        f"Read the ticket below and produce a numbered implementation plan.\n\n"
        f"Requirements for the plan:\n"
        f"- Reference concrete file paths (relative to the repo root) that you would touch.\n"
        f"- TARGET-REPO DISCIPLINE (read carefully): the plan must be IMPLEMENTABLE\n"
        f"  in this target repo ({resolved_repo_path}). The implementer is a\n"
        f"  separate Claude Code run that runs INSIDE this repo and CAN ONLY\n"
        f"  edit files here. So:\n"
        f"     - If the ticket's actual work belongs primarily in this repo →\n"
        f"       write the plan for this repo. Steps may still mention sibling\n"
        f"       files (read-only) when the work depends on them.\n"
        f"     - If the ticket's actual work belongs primarily in a SIBLING\n"
        f"       repo (you discovered this by peeking sibling code) → STOP and\n"
        f"       say so in the intro paragraph: \"This ticket appears to be\n"
        f"       filed against the wrong repo — the implementation belongs in\n"
        f"       <sibling>. Recommend re-filing as a ticket in <sibling>.\"\n"
        f"       Then either (a) write a small in-this-repo plan for whatever\n"
        f"       fragment IS in scope here, or (b) if nothing in this repo,\n"
        f"       output zero numbered steps and explain.\n"
        f"       AND: when you produce a wrong-repo response, append a new line\n"
        f"       at the very end of your output (after SUMMARY, if present):\n"
        f"           TARGET_REPO_MISMATCH: true\n"
        f"       This signals the platform to suppress the `selected_repo`\n"
        f"       context emission for this turn — without it, the wrong-repo\n"
        f"       run silently overwrites the prior turn's correct selection,\n"
        f"       and subsequent turns default to the wrong repo too.\n"
        f"     - Do NOT write the bulk of the plan against sibling files. The\n"
        f"       implementer cannot execute it — the implementation will\n"
        f"       silently produce zero changes.\n"
        f"- MULTI-REPO LABELING (only when plan steps span repos): if the plan\n"
        f"  contains steps in this target repo AND also describes follow-up\n"
        f"  steps in sibling repos that the user must do separately, label\n"
        f"  EACH numbered step with `**Repo:** <name>` (use the workspace\n"
        f"  `name` — e.g. `another_agent_backend`, `another_agent_frontend`).\n"
        f"  Sibling-repo steps must be flagged `**Repo:** <name> (out of scope\n"
        f"  for this implementation — separate ticket required)`. For\n"
        f"  single-repo plans, the label is optional (every step is in this\n"
        f"  repo by construction).\n"
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


def _run_planning_job(
    job_id: str,
    ticket_number: int,
    ticket_body: str,
    resolved_repo_path: str,
    timeout_seconds: int | None = None,
) -> None:
    """Runs the actual Claude planning subprocess. Called in a BackgroundTask
    so the HTTP response returns immediately with the job_id.

    ``timeout_seconds`` is the optional per-request override from the
    request schema; when None, falls back to PLANNING_TIMEOUT_SECONDS.
    The schema already caps the override at 1800s, so no extra clamp
    needed here."""
    workspace_dir = os.path.dirname(resolved_repo_path.rstrip("/"))
    effective_timeout = timeout_seconds or PLANNING_TIMEOUT_SECONDS
    prompt = _build_planning_prompt(
        ticket_number=ticket_number,
        ticket_body=ticket_body,
        resolved_repo_path=resolved_repo_path,
        workspace_dir=workspace_dir,
    )

    log.info(
        "instruct_planning.running_claude",
        job_id=job_id,
        ticket_number=ticket_number,
        model=CLAUDE_MODEL,
        repo_path=resolved_repo_path,
        workspace_dir=workspace_dir,
        timeout_seconds=effective_timeout,
    )

    result = claude_runner.run_blocking(
        args=claude_runner.build_claude_args(prompt=prompt, model=CLAUDE_MODEL),
        cwd=resolved_repo_path,
        timeout_seconds=effective_timeout,
        job_id=job_id,
    )

    if result.spawn_error:
        log.error("instruct_planning.spawn_failed", job_id=job_id, err=result.spawn_error)
        job_manager.mark_failed(job_id, result.spawn_error, kind=SPAWN_FAILED)
        return

    duration = result.duration_seconds

    if result.timed_out:
        log.error(
            "instruct_planning.timeout",
            job_id=job_id,
            ticket_number=ticket_number,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, f"timeout after {duration}s", kind=TIMEOUT)
        return

    # Cancel arrived during the run → SIGTERM/SIGKILL killed Claude;
    # whatever stdout it produced is incomplete and not worth surfacing.
    if result.cancelled:
        log.info(
            "instruct_planning.cancelled",
            job_id=job_id,
            ticket_number=ticket_number,
            duration_seconds=duration,
        )
        job_manager.mark_failed(job_id, "cancelled by client", kind=CANCELLED)
        return

    if result.returncode != 0:
        stderr_text = result.stderr.strip()
        log.error(
            "instruct_planning.claude_failed",
            job_id=job_id,
            ticket_number=ticket_number,
            exit_code=result.returncode,
            stderr=stderr_text,
            duration_seconds=duration,
        )
        job_manager.mark_failed(
            job_id,
            stderr_text or f"claude exited with code {result.returncode}",
            kind=CLAUDE_FAILED,
        )
        return

    raw_output = result.stdout.strip()
    # Detect + strip wrong-repo flag BEFORE splitting plan/summary so the
    # marker doesn't leak into either field. When present, we skip the
    # selected_repo context emission below — claiming "this repo" as
    # selected when Claude itself said "this is the wrong repo" would
    # corrupt the conversation's repo context for every subsequent turn.
    target_repo_mismatch = detect_target_repo_mismatch(raw_output)
    cleaned_output = strip_target_repo_mismatch_marker(raw_output)
    plan, summary = split_plan_and_summary(cleaned_output)
    log.info(
        "instruct_planning.ok",
        job_id=job_id,
        ticket_number=ticket_number,
        plan_len=len(plan),
        summary_present=summary is not None,
        target_repo_mismatch=target_repo_mismatch,
        duration_seconds=duration,
    )
    payload: dict[str, Any] = {
        "plan": plan,
        "ticket_number": ticket_number,
        "repo_path": resolved_repo_path,
        "exit_code": 0,
        "duration_seconds": duration,
        # The platform strips __label__ from the LLM-visible response
        # body and uses it as the artifact's dedup key. Plan iterations
        # for the same (repo, ticket_number) share this label, so the
        # next-turn [Conversation artifacts] block shows ONLY the latest
        # plan (older versions stay in storage and remain fetchable by
        # id). Repo is included so the same ticket number filed in two
        # different repos doesn't collide — `plan-another_coder-2`
        # and `plan-another_agent_backend-2` are independent.
        "__label__": (
            f"plan-{os.path.basename(resolved_repo_path.rstrip('/'))}"
            f"-{ticket_number}"
        ),
    }
    # __context__ is emitted only when this run actually owns the work.
    # When Claude Code flagged TARGET_REPO_MISMATCH, this run produced
    # no actionable plan — claiming "this repo" as selected_repo would
    # silently overwrite the prior turn's correct selection and cause
    # subsequent refinements to default to the wrong repo. Suppress.
    if not target_repo_mismatch:
        # The platform strips __context__ from the LLM-visible response
        # body and persists each key as a context artifact on the
        # assistant message. Next turn's prompt injects them as
        # [Conversation context] so the Developer Agent reads
        # selected_repo.path directly instead of parsing markdown.
        payload["__context__"] = {
            "selected_repo": _build_selected_repo_context(resolved_repo_path),
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
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_INSTRUCT)
async def instruct_planning(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = extract_args(body)
    log.info(
        "instruct_planning.request",
        ticket_number=args.get("ticket_number"),
        ticket_body_len=len(args["ticket_body"]) if isinstance(args.get("ticket_body"), str) else None,
        repo_path=args.get("repo_path"),
    )

    # PlanningRequest enforces ticket_number int-coercion, non-empty
    # ticket_body, and max-length caps. Validation errors come back with
    # the LLM-recovery hints intact so the Planner Agent can self-
    # correct on its next pass — see routers/_schemas.py for why this
    # path returns 200 + {error: string} rather than FastAPI's 422.
    try:
        req = PlanningRequest.model_validate(args)
    except ValidationError as e:
        return {"error": first_error_message(e)}

    resolved_repo_path = req.repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    resolved_repo_path, err = validate_repo_path(resolved_repo_path)
    if err:
        return {"error": err}

    job = job_manager.create(kind="instruct_planning")
    background_tasks.add_task(
        _run_planning_job,
        job.job_id,
        req.ticket_number,
        req.ticket_body,
        resolved_repo_path,
        req.timeout_seconds,
    )

    log.info("instruct_planning.dispatched", job_id=job.job_id, ticket_number=req.ticket_number)
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
