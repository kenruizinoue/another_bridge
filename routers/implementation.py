import os
import subprocess
import time
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import ValidationError

from config import CLAUDE_MODEL, CODING_REPO_PATH, GITHUB_DEFAULT_REPO, GITHUB_PAT
from jobs import job_manager
from routers._schemas import ImplementationRequest, first_error_message
from routers.repos import validate_repo_path
from services import claude_runner, github_service
from services.errors import (
    CANCELLED,
    CLAUDE_FAILED,
    GIT_PUSH_FAILED,
    PR_CREATE_FAILED,
    SPAWN_FAILED,
    TIMEOUT,
)
from services.git_service import (
    _branch_exists_locally,
    _branch_exists_on_remote,
    _git_origin_url,
    _parse_owner_repo,
    _resolve_base_branch,
    _run_git,
)
from services.github_service import format_pr_creation_error
from services.repo_context import build_selected_repo_context
from services.request import extract_args

# Re-exports — tests in test_implementation_pr_creation patch
# routers.implementation._git_origin_url and import
# resolve_pr_repo_slug / format_pr_creation_error from this module.
# Keeping the names accessible here means those tests don't have to
# learn the new service module path.
__all__ = [
    "router",
    "format_pr_creation_error",
    "resolve_pr_repo_slug",
    "_git_origin_url",
    "_parse_owner_repo",
]

# Cross-router re-export so other code that imported _build_selected_repo_context
# from this module (none today, but planning.py used to be the source) keeps
# working.
_build_selected_repo_context = build_selected_repo_context

log = structlog.get_logger()

router = APIRouter()

# 30 minutes — matches the platform's per-tool pollMaxSeconds (1800s) on
# the async-webhook EM template, so a long implementation pass isn't
# cut short here before the platform's poll budget would have allowed
# it to complete.
IMPLEMENTATION_TIMEOUT_SECONDS = 1800
PUSH_TIMEOUT_SECONDS = 120


def resolve_pr_repo_slug(
    repo_path: str, github_default_repo: str
) -> str | None:
    """Derive the GitHub `owner/repo` slug for PR creation.

    Prefers the local clone's `origin` remote URL over GITHUB_DEFAULT_REPO,
    because the local origin is the source of truth — it's what `git push`
    writes to. Using a different slug for PR creation when working across
    multiple repos in WORKSPACE_ROOT produces "GitHub PR API 422: head
    invalid" because the branch was pushed to a different repo.

    Falls back to GITHUB_DEFAULT_REPO when the local origin can't be parsed
    (no origin configured, non-GitHub URL, malformed). Returns None when
    neither source yields a usable slug — caller should fail loud rather
    than guess.

    Defined in this router (not services/github_service) because it
    crosses git origin lookup + PR-target selection. The local
    `_git_origin_url` binding is the one tests patch — keeping the
    function here means those patches still resolve correctly without
    forcing test rewrites.
    """
    origin_url = _git_origin_url(repo_path)
    derived_slug = _parse_owner_repo(origin_url) if origin_url else None
    return derived_slug or (github_default_repo or None)


def _build_implementation_prompt(
    ticket_number: int,
    ticket_body: str,
    plan: str,
    resolved_repo_path: str,
    branch_name: str,
    base_branch: str,
    workspace_dir: str,
) -> str:
    return (
        f"You are implementing Ticket #{ticket_number} in the repo at {resolved_repo_path}.\n"
        f"You are currently on branch {branch_name}, branched from {base_branch}.\n\n"
        f"TICKET:\n{ticket_body.strip()}\n\n"
        f"PLAN (follow this exactly — another agent already approved it):\n{plan.strip()}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Implement the plan by editing files in this repo. You have full read/write permission here.\n"
        f"- Sibling repos live under {workspace_dir} (e.g. ../<sibling>). You may READ them but must NOT modify anything outside this repo.\n"
        f"- COMMIT PER STEP: the plan above is numbered. Make ONE git commit\n"
        f"  per numbered step, IN PLAN ORDER, so the branch's commit history\n"
        f"  mirrors the plan exactly. After finishing each step:\n"
        f"     git add -A\n"
        f"     git commit -m \"#{ticket_number} step <N>: <step's leading imperative phrase>\"\n"
        f"  e.g. for step 3 \"Add UserSchema with email validation\":\n"
        f"     git commit -m \"#{ticket_number} step 3: Add UserSchema with email validation\"\n"
        f"  Reasons:\n"
        f"     - Reviewers walking the PR see the plan's order in `git log`.\n"
        f"     - If a step breaks something, `git bisect` / revert is per-step.\n"
        f"     - The plan and the branch stay traceable 1:1.\n"
        f"  Do NOT batch multiple steps into one commit. Do NOT split a single\n"
        f"  step across multiple commits — if one step's edits don't cleanly\n"
        f"  fit one commit, the plan was wrong; commit what makes sense and\n"
        f"  call it out in your final output.\n"
        f"- If you finish with uncommitted changes (files Claude touched but\n"
        f"  didn't commit), the platform will auto-commit them as a fallback,\n"
        f"  but that loses the per-step structure — avoid relying on it.\n"
        f"- Do NOT push the branch. Do NOT open a pull request. A later step handles that.\n\n"
        f"WHAT YOU MUST NOT RUN:\n"
        f"- DO NOT start any dev server: no `npm run dev`, `npm start`, `yarn dev`, `pnpm dev`, `uvicorn`,\n"
        f"  `python main.py`, `next dev`, `vite`, `rails server`, etc. These hang forever in headless mode.\n"
        f"- DO NOT call any local HTTP service (e.g. localhost:3000, 127.0.0.1:8000). The backend is\n"
        f"  not running here. Any curl/fetch to a local port will fail or block.\n"
        f"- DO NOT install packages over the network unless the plan explicitly requires it\n"
        f"  (no `npm install`, `pip install`, `brew install` unless the plan calls it out).\n"
        f"- DO NOT spawn any long-running or interactive process (no watchers, no REPLs, no `gh auth login`).\n\n"
        f"WHAT YOU MAY RUN (only if the plan calls for it):\n"
        f"- Static checks: type-checkers (`tsc --noEmit`, `mypy`), linters (`eslint`, `ruff`), formatters.\n"
        f"- Unit tests: `npm test -- --run`, `jest --runInBand`, `pytest -x` — only if the plan explicitly says to.\n"
        f"- Read-only git commands and local file reads.\n\n"
        f"- If you hit a blocker you cannot resolve, commit whatever works and explain the blocker in your final output."
    )


def _run_implementation_job(
    job_id: str,
    ticket_number: int,
    ticket_body: str,
    plan: str,
    resolved_repo_path: str,
    base_branch_override: str | None = None,
    timeout_seconds: int | None = None,
) -> None:
    """All git + Claude + push + PR work runs here in a BackgroundTask. Updates
    job_manager throughout so the platform poller sees real-time status."""
    started = time.time()
    branch_name = f"agent/ticket-{ticket_number}"

    def fail(msg: str, *, kind: str | None = None, **extra: Any) -> None:
        """Log + mark_failed with optional structured kind. Generic
        git failures (status, branch resolution, dirty tree) leave
        kind=None — they're operator/agent issues that don't map to
        the canonical Claude/push/PR failure modes."""
        log.error("instruct_implementation.failed", job_id=job_id, error=msg, **extra)
        job_manager.mark_failed(job_id, msg, kind=kind)

    git_dir_check = _run_git(["rev-parse", "--git-dir"], resolved_repo_path)
    if git_dir_check.returncode != 0:
        fail(f"{resolved_repo_path} is not a git repository")
        return

    status_check = _run_git(["status", "--porcelain"], resolved_repo_path)
    if status_check.returncode != 0:
        fail(f"git status failed: {status_check.stderr.strip()}")
        return
    if status_check.stdout.strip():
        fail(
            "working tree is not clean — commit or stash your changes before implementing",
            status_output=status_check.stdout.strip()[:500],
        )
        return

    base_branch, detect_err = _resolve_base_branch(resolved_repo_path, base_branch_override)
    if detect_err or base_branch is None:
        fail(detect_err or "base branch resolution failed")
        return

    if _branch_exists_locally(branch_name, resolved_repo_path):
        fail(f"branch '{branch_name}' already exists locally — delete it or bump the ticket")
        return
    if _branch_exists_on_remote(branch_name, resolved_repo_path):
        fail(f"branch '{branch_name}' already exists on origin — delete it or bump the ticket")
        return

    checkout_base = _run_git(["checkout", base_branch], resolved_repo_path)
    if checkout_base.returncode != 0:
        fail(f"could not checkout {base_branch}: {checkout_base.stderr.strip()}")
        return

    pull = _run_git(["pull", "--ff-only", "origin", base_branch], resolved_repo_path)
    if pull.returncode != 0:
        fail(f"could not pull latest {base_branch}: {pull.stderr.strip()}")
        return

    create = _run_git(["checkout", "-b", branch_name], resolved_repo_path)
    if create.returncode != 0:
        fail(f"could not create branch {branch_name}: {create.stderr.strip()}")
        return

    log.info(
        "instruct_implementation.branch_created",
        job_id=job_id,
        branch_name=branch_name,
        base_branch=base_branch,
        repo_path=resolved_repo_path,
    )

    workspace_dir = os.path.dirname(resolved_repo_path.rstrip("/"))
    implement_prompt = _build_implementation_prompt(
        ticket_number=ticket_number,
        ticket_body=ticket_body,
        plan=plan,
        resolved_repo_path=resolved_repo_path,
        branch_name=branch_name,
        base_branch=base_branch,
        workspace_dir=workspace_dir,
    )

    log.info(
        "instruct_implementation.running_claude",
        job_id=job_id,
        ticket_number=ticket_number,
        model=CLAUDE_MODEL,
        repo_path=resolved_repo_path,
        branch_name=branch_name,
    )

    effective_timeout = timeout_seconds or IMPLEMENTATION_TIMEOUT_SECONDS
    result = claude_runner.run_blocking(
        args=claude_runner.build_claude_args(prompt=implement_prompt, model=CLAUDE_MODEL),
        cwd=resolved_repo_path,
        timeout_seconds=effective_timeout,
        job_id=job_id,
    )

    if result.spawn_error:
        fail(
            result.spawn_error,
            kind=SPAWN_FAILED,
            branch_name=branch_name,
            duration_seconds=round(time.time() - started, 2),
        )
        return

    if result.timed_out:
        fail(
            "claude timed out",
            kind=TIMEOUT,
            branch_name=branch_name,
            duration_seconds=round(time.time() - started, 2),
        )
        return

    # Cancel arrived during the run → reflect it in job status. Any
    # half-written branch + working-tree changes Claude left behind are
    # NOT cleaned up here; that's a follow-up if it becomes a problem.
    if result.cancelled:
        log.info("instruct_implementation.cancelled", job_id=job_id, ticket_number=ticket_number, branch_name=branch_name)
        job_manager.mark_failed(job_id, "cancelled by client", kind=CANCELLED)
        return

    if result.returncode != 0:
        stderr = result.stderr.strip()
        fail(
            stderr or f"claude exited with code {result.returncode}",
            kind=CLAUDE_FAILED,
            exit_code=result.returncode,
            branch_name=branch_name,
        )
        return

    # Auto-commit fallback: if Claude left the tree dirty, commit so branch is clean
    status_after = _run_git(["status", "--porcelain"], resolved_repo_path)
    if status_after.stdout.strip():
        log.info("instruct_implementation.auto_commit_leftover", job_id=job_id, ticket_number=ticket_number)
        _run_git(["add", "-A"], resolved_repo_path)
        _run_git(
            ["commit", "-m", f"Implement ticket #{ticket_number} (auto-commit fallback)"],
            resolved_repo_path,
        )

    commits_count_raw = _run_git(["rev-list", "--count", f"origin/{base_branch}..HEAD"], resolved_repo_path)
    try:
        commits = int(commits_count_raw.stdout.strip() or "0")
    except ValueError:
        commits = 0

    files_diff = _run_git(["diff", "--name-only", f"origin/{base_branch}..HEAD"], resolved_repo_path)
    files_changed = [line for line in files_diff.stdout.splitlines() if line.strip()]

    if commits == 0 and not files_changed:
        fail(
            "claude produced no commits and no file changes",
            branch_name=branch_name,
            claude_output=result.stdout.strip()[:500],
        )
        return

    push_result = subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        capture_output=True,
        text=True,
        cwd=resolved_repo_path,
        timeout=PUSH_TIMEOUT_SECONDS,
    )
    if push_result.returncode != 0:
        fail(
            f"git push failed: {push_result.stderr.strip()}",
            kind=GIT_PUSH_FAILED,
            branch_name=branch_name,
        )
        return

    log.info("instruct_implementation.pushed", job_id=job_id, branch_name=branch_name)

    # Open PR via GitHub REST API. See resolve_pr_repo_slug for why we
    # derive the slug from the local clone's origin rather than from
    # GITHUB_DEFAULT_REPO (multi-repo workspaces hit "head invalid"
    # otherwise).
    repo_slug = resolve_pr_repo_slug(resolved_repo_path, GITHUB_DEFAULT_REPO)
    if not repo_slug:
        fail(
            "could not determine GitHub repo slug — local clone has no parseable "
            "origin remote and GITHUB_DEFAULT_REPO is not configured. Set the "
            "remote: cd '{}' && git remote add origin <https://github.com/...>"
            .format(resolved_repo_path),
            branch_name=branch_name,
        )
        return

    plan_excerpt = plan.strip()
    if len(plan_excerpt) > 4000:
        plan_excerpt = plan_excerpt[:4000] + "\n\n_(plan truncated at 4000 chars)_"
    pr_body = (
        f"Automated implementation of ticket #{ticket_number}.\n\n"
        f"- **Issue:** https://github.com/{repo_slug}/issues/{ticket_number}\n"
        f"- **Base branch:** `{base_branch}`\n"
        f"- **Commits:** {commits}\n"
        f"- **Files changed:** {len(files_changed)}\n\n"
        f"## Plan followed\n\n{plan_excerpt}\n\n"
        f"---\n"
        f"🤖 Generated by AnotherAgent Coder Team"
    )
    pr_title = f"Ticket #{ticket_number}: agent implementation"

    if not GITHUB_PAT:
        fail("GITHUB_PAT not configured — cannot create PR", branch_name=branch_name)
        return

    pr_resp = github_service.create_pr(
        repo_slug=repo_slug,
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base=base_branch,
    )
    if not pr_resp.ok:
        fail(
            format_pr_creation_error(
                pr_resp.status_code,
                pr_resp.text,
                branch_name,
                repo_slug,
                base_branch,
                resolved_repo_path,
            ),
            kind=PR_CREATE_FAILED,
            branch_name=branch_name,
            commits=commits,
            files_changed=files_changed,
        )
        return

    pr_data = pr_resp.json()
    pr_url = pr_data.get("html_url")
    pr_number = pr_data.get("number")

    checkout_back = _run_git(["checkout", base_branch], resolved_repo_path)
    if checkout_back.returncode != 0:
        log.warning(
            "instruct_implementation.cleanup_checkout_failed",
            job_id=job_id,
            base_branch=base_branch,
            stderr=checkout_back.stderr.strip(),
        )

    total_duration = round(time.time() - started, 2)
    log.info(
        "instruct_implementation.ok",
        job_id=job_id,
        ticket_number=ticket_number,
        branch_name=branch_name,
        commits=commits,
        files_changed_count=len(files_changed),
        pr_number=pr_number,
        pr_url=pr_url,
        duration_seconds=total_duration,
    )

    job_manager.mark_done(
        job_id,
        {
            "ticket_number": ticket_number,
            "branch_name": branch_name,
            "base_branch": base_branch,
            "repo_path": resolved_repo_path,
            "commits": commits,
            "files_changed": files_changed,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "claude_output": result.stdout.strip(),
            "duration_seconds": total_duration,
            # Cross-turn context for the platform — see planning.py for the
            # full mechanism. instruct_implementation knows more than
            # planning does (the branch + the PR URL), so we surface those
            # too in case a downstream agent on a later turn needs them.
            "__context__": {
                "selected_repo": build_selected_repo_context(resolved_repo_path),
                "current_branch": branch_name,
                "last_pr_url": pr_url,
            },
        },
    )


@router.post("/tools/instruct_implementation")
async def instruct_implementation(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = extract_args(body)
    log.info(
        "instruct_implementation.request",
        ticket_number=args.get("ticket_number"),
        ticket_body_len=len(args["ticket_body"]) if isinstance(args.get("ticket_body"), str) else None,
        plan_len=len(args["plan"]) if isinstance(args.get("plan"), str) else None,
        repo_path=args.get("repo_path"),
        base_branch_override=args.get("base_branch"),
    )

    # ImplementationRequest enforces ticket_number/body/plan and caps
    # all string fields. Empty/whitespace base_branch is normalized to
    # None inside the schema so the auto-detection path runs. See
    # routers/_schemas.py for why this returns 200 + {error: string}
    # rather than FastAPI's default 422.
    try:
        req = ImplementationRequest.model_validate(args)
    except ValidationError as e:
        return {"error": first_error_message(e)}

    resolved_repo_path = req.repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    resolved_repo_path, err = validate_repo_path(resolved_repo_path)
    if err:
        return {"error": err}

    job = job_manager.create(kind="instruct_implementation")
    background_tasks.add_task(
        _run_implementation_job,
        job.job_id,
        req.ticket_number,
        req.ticket_body,
        req.plan,
        resolved_repo_path,
        req.base_branch,
        req.timeout_seconds,
    )

    log.info("instruct_implementation.dispatched", job_id=job.job_id, ticket_number=ticket_number)
    return {
        "job_id": job.job_id,
        "kind": "instruct_implementation",
        "status": "running",
        "status_url": f"/jobs/{job.job_id}/status",
        # See planning.py for the cancel-propagation contract.
        "cancel_url": f"/jobs/{job.job_id}/cancel",
    }
