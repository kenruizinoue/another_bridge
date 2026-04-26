import os
import subprocess
import time
from typing import Any

import requests
import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from config import BASE_BRANCH, CLAUDE_MODEL, CODING_REPO_PATH, GITHUB_API_BASE, GITHUB_DEFAULT_REPO, GITHUB_PAT
from routers.planning import _build_selected_repo_context
from routers.repos import _git_origin_url, _parse_owner_repo, validate_repo_path
from jobs import job_manager

log = structlog.get_logger()

router = APIRouter()

GIT_TIMEOUT_SECONDS = 60
# 30 minutes — matches the platform's per-tool pollMaxSeconds (1800s) on
# the async-webhook EM template, so a long implementation pass isn't
# cut short here before the platform's poll budget would have allowed
# it to complete.
IMPLEMENTATION_TIMEOUT_SECONDS = 1800
PUSH_TIMEOUT_SECONDS = 120


def _extract_args(body: Any) -> dict[str, Any]:
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}
    return args


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _branch_exists_locally(branch: str, cwd: str) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", branch], cwd).returncode == 0


def _branch_exists_on_remote(branch: str, cwd: str) -> bool:
    return _run_git(["ls-remote", "--exit-code", "--heads", "origin", branch], cwd).returncode == 0


def _detect_default_branch(cwd: str) -> str | None:
    """Read the remote's default branch via `git ls-remote --symref origin HEAD`.

    Output looks like:
        ref: refs/heads/main	HEAD
        abc123...	HEAD

    Returns the branch name (e.g. "main") or None if detection fails (no
    network, no origin, malformed output, etc.). Never raises — callers
    fall back to BASE_BRANCH.
    """
    result = _run_git(["ls-remote", "--symref", "origin", "HEAD"], cwd)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        # Match `ref: refs/heads/<name>\tHEAD`
        if line.startswith("ref:") and "refs/heads/" in line:
            try:
                ref_part = line.split("ref:", 1)[1].strip().split("\t", 1)[0].strip()
                # ref_part = "refs/heads/main"
                if ref_part.startswith("refs/heads/"):
                    return ref_part[len("refs/heads/") :]
            except (IndexError, ValueError):
                continue
    return None


def _resolve_base_branch(
    cwd: str, override: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve the base branch to use for an implementation, in order:

    1. `override` (caller-supplied, e.g. agent passed `base_branch=feat/foo`)
    2. The remote's default branch via _detect_default_branch (zero-config —
       works automatically across repos that use main/master/dev/etc.)
    3. `BASE_BRANCH` env (legacy fallback for pre-detection deployments)

    Each candidate must exist on origin. If a higher-priority candidate
    is set but doesn't exist, return that error verbatim — don't silently
    skip to the next, because the user/operator's intent should win.
    """
    # 1. Explicit caller override takes priority
    if override:
        if not _branch_exists_on_remote(override, cwd):
            return None, f"requested base_branch '{override}' does not exist on origin"
        return override, None

    # 2. Auto-detect from origin's HEAD symref (zero-config path)
    detected = _detect_default_branch(cwd)
    if detected:
        # _detect_default_branch already proved the ref exists on origin
        # (it came from origin's symref response). Skip the existence check.
        return detected, None

    # 3. Legacy fallback: BASE_BRANCH env (kept for deployments that
    #    relied on it before auto-detect existed).
    if BASE_BRANCH:
        if not _branch_exists_on_remote(BASE_BRANCH, cwd):
            return None, (
                f"could not auto-detect default branch and BASE_BRANCH "
                f"fallback '{BASE_BRANCH}' does not exist on origin"
            )
        return BASE_BRANCH, None

    return None, (
        "could not auto-detect remote default branch and no BASE_BRANCH "
        "env or base_branch arg provided"
    )


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
    """
    origin_url = _git_origin_url(repo_path)
    derived_slug = _parse_owner_repo(origin_url) if origin_url else None
    return derived_slug or (github_default_repo or None)


def format_pr_creation_error(
    status_code: int,
    response_text: str,
    branch_name: str,
    repo_slug: str,
    base_branch: str,
    repo_path: str,
) -> str:
    """Format the PR-creation error message returned to the user.

    Enriches the canonical "branch was pushed but PR API can't see it"
    case (HTTP 422 with `field:head, code:invalid`) with diagnose+fix
    commands and a manual-PR URL, so the user can resolve in 30 seconds
    without digging through coder logs. With the resolve_pr_repo_slug
    fix in place, the only remaining way to hit this error is a renamed
    GitHub repo with a stale local origin.

    Other errors pass through verbatim.
    """
    is_head_invalid = (
        status_code == 422
        and '"field":"head"' in response_text
        and '"code":"invalid"' in response_text
    )
    if is_head_invalid:
        return (
            f"GitHub PR API 422 (head invalid): branch '{branch_name}' was "
            f"pushed to {repo_slug} but the PR creation API can't see it. "
            f"This usually means the repo was renamed on GitHub but your "
            f"local clone's origin URL still points at the old name.\n"
            f"\n"
            f"Diagnose:  cd '{repo_path}' && git remote -v\n"
            f"Fix:       cd '{repo_path}' && git remote set-url "
            f"origin https://github.com/<correct-owner>/<correct-repo>.git\n"
            f"\n"
            f"Your code changes ARE on GitHub at branch '{branch_name}' — "
            f"you can open the PR manually if you prefer not to retry. "
            f"Visit: https://github.com/{repo_slug}/compare/{base_branch}..."
            f"{branch_name}\n"
            f"\n"
            f"Original API response: {response_text}"
        )
    return f"GitHub PR API {status_code}: {response_text}"


def _run_implementation_job(
    job_id: str,
    ticket_number: int,
    ticket_body: str,
    plan: str,
    resolved_repo_path: str,
    base_branch_override: str | None = None,
) -> None:
    """All git + Claude + push + PR work runs here in a BackgroundTask. Updates
    job_manager throughout so the platform poller sees real-time status."""
    started = time.time()
    branch_name = f"agent/ticket-{ticket_number}"

    def fail(msg: str, **extra: Any) -> None:
        log.error("instruct_implementation.failed", job_id=job_id, error=msg, **extra)
        job_manager.mark_failed(job_id, msg)

    # Verify it's actually a git repo
    git_dir_check = _run_git(["rev-parse", "--git-dir"], resolved_repo_path)
    if git_dir_check.returncode != 0:
        fail(f"{resolved_repo_path} is not a git repository")
        return

    # Verify working tree is clean
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

    # Resolve configured base branch (dev by default)
    base_branch, detect_err = _resolve_base_branch(resolved_repo_path, base_branch_override)
    if detect_err or base_branch is None:
        fail(detect_err or "base branch resolution failed")
        return

    # Abort early if the target branch already exists anywhere
    if _branch_exists_locally(branch_name, resolved_repo_path):
        fail(f"branch '{branch_name}' already exists locally — delete it or bump the ticket")
        return
    if _branch_exists_on_remote(branch_name, resolved_repo_path):
        fail(f"branch '{branch_name}' already exists on origin — delete it or bump the ticket")
        return

    # Sync base branch
    checkout_base = _run_git(["checkout", base_branch], resolved_repo_path)
    if checkout_base.returncode != 0:
        fail(f"could not checkout {base_branch}: {checkout_base.stderr.strip()}")
        return

    pull = _run_git(["pull", "--ff-only", "origin", base_branch], resolved_repo_path)
    if pull.returncode != 0:
        fail(f"could not pull latest {base_branch}: {pull.stderr.strip()}")
        return

    # Create the working branch
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

    # Run Claude
    workspace_dir = os.path.dirname(resolved_repo_path.rstrip("/"))
    implement_prompt = (
        f"You are implementing Ticket #{ticket_number} in the repo at {resolved_repo_path}.\n"
        f"You are currently on branch {branch_name}, branched from {base_branch}.\n\n"
        f"TICKET:\n{ticket_body.strip()}\n\n"
        f"PLAN (follow this exactly — another agent already approved it):\n{plan.strip()}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Implement the plan by editing files in this repo. You have full read/write permission here.\n"
        f"- Sibling repos live under {workspace_dir} (e.g. ../<sibling>). You may READ them but must NOT modify anything outside this repo.\n"
        f"- When done with all file changes, make ONE commit:\n"
        f"    git add -A && git commit -m \"Implement ticket #{ticket_number}: <short summary>\"\n"
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

    log.info(
        "instruct_implementation.running_claude",
        job_id=job_id,
        ticket_number=ticket_number,
        model=CLAUDE_MODEL,
        repo_path=resolved_repo_path,
        branch_name=branch_name,
    )

    try:
        claude_result = subprocess.run(
            [
                "claude",
                "-p",
                implement_prompt,
                "--model",
                CLAUDE_MODEL,
                "--output-format",
                "text",
                "--dangerously-skip-permissions",
            ],
            capture_output=True,
            text=True,
            timeout=IMPLEMENTATION_TIMEOUT_SECONDS,
            cwd=resolved_repo_path,
        )
    except subprocess.TimeoutExpired:
        fail("claude timed out", branch_name=branch_name, duration_seconds=round(time.time() - started, 2))
        return

    if claude_result.returncode != 0:
        stderr = (claude_result.stderr or "").strip()
        fail(
            stderr or f"claude exited with code {claude_result.returncode}",
            exit_code=claude_result.returncode,
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

    # Count commits + enumerate files changed vs base
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
            claude_output=(claude_result.stdout or "").strip()[:500],
        )
        return

    # Push the branch
    push_result = subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        capture_output=True,
        text=True,
        cwd=resolved_repo_path,
        timeout=PUSH_TIMEOUT_SECONDS,
    )
    if push_result.returncode != 0:
        fail(f"git push failed: {push_result.stderr.strip()}", branch_name=branch_name)
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

    pr_resp = requests.post(
        f"{GITHUB_API_BASE}/repos/{repo_slug}/pulls",
        headers={
            "Authorization": f"Bearer {GITHUB_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": pr_title,
            "body": pr_body,
            "head": branch_name,
            "base": base_branch,
        },
        timeout=30,
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
            branch_name=branch_name,
            commits=commits,
            files_changed=files_changed,
        )
        return

    pr_data = pr_resp.json()
    pr_url = pr_data.get("html_url")
    pr_number = pr_data.get("number")

    # Switch back to base so the repo is ready for the next ticket
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
            "claude_output": (claude_result.stdout or "").strip(),
            "duration_seconds": total_duration,
            # Cross-turn context for the platform — see planning.py for the
            # full mechanism. instruct_implementation knows more than
            # planning does (the branch + the PR URL), so we surface those
            # too in case a downstream agent on a later turn needs them.
            "__context__": {
                "selected_repo": _build_selected_repo_context(resolved_repo_path),
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

    args = _extract_args(body)
    ticket_number_raw = args.get("ticket_number")
    ticket_body = args.get("ticket_body")
    plan = args.get("plan")
    repo_path = args.get("repo_path")
    # Optional explicit override — when omitted, _resolve_base_branch
    # auto-detects the remote's default branch via ls-remote --symref.
    base_branch_override_raw = args.get("base_branch")
    base_branch_override = (
        base_branch_override_raw.strip()
        if isinstance(base_branch_override_raw, str) and base_branch_override_raw.strip()
        else None
    )

    log.info(
        "instruct_implementation.request",
        ticket_number=ticket_number_raw,
        ticket_body_len=len(ticket_body) if isinstance(ticket_body, str) else None,
        plan_len=len(plan) if isinstance(plan, str) else None,
        repo_path=repo_path,
        base_branch_override=base_branch_override,
    )

    # Fast input validation — return errors immediately, don't burn a job_id
    try:
        ticket_number = int(ticket_number_raw)
    except (TypeError, ValueError):
        return {"error": "ticket_number is required and must be an integer"}

    if not isinstance(ticket_body, str) or not ticket_body.strip():
        return {"error": "ticket_body is required and must be a non-empty string"}

    if not isinstance(plan, str) or not plan.strip():
        return {"error": "plan is required and must be a non-empty string"}

    resolved_repo_path = repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    resolved_repo_path, err = validate_repo_path(resolved_repo_path)
    if err:
        return {"error": err}

    # Dispatch the heavy work to a background task; return job_id immediately
    job = job_manager.create(kind="instruct_implementation")
    background_tasks.add_task(
        _run_implementation_job,
        job.job_id,
        ticket_number,
        ticket_body,
        plan,
        resolved_repo_path,
        base_branch_override,
    )

    log.info("instruct_implementation.dispatched", job_id=job.job_id, ticket_number=ticket_number)
    return {
        "job_id": job.job_id,
        "kind": "instruct_implementation",
        "status": "running",
        "status_url": f"/jobs/{job.job_id}/status",
    }
