import os
import subprocess
from typing import Any

import structlog
from fastapi import APIRouter, Request

from config import BASE_BRANCH, CODING_REPO_PATH

log = structlog.get_logger()

router = APIRouter()

GIT_TIMEOUT_SECONDS = 60


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


def _resolve_base_branch(cwd: str) -> tuple[str | None, str | None]:
    """Return (branch_name, error). Uses the BASE_BRANCH config (default 'dev')
    and verifies it exists on origin. We intentionally do NOT fall back to
    main — main is reserved for releases in Ken's workspace and agent PRs must
    target the integration branch."""
    if not BASE_BRANCH:
        return None, "BASE_BRANCH is not configured"
    if not _branch_exists_on_remote(BASE_BRANCH, cwd):
        return None, f"configured BASE_BRANCH '{BASE_BRANCH}' does not exist on origin"
    return BASE_BRANCH, None


def _branch_exists_locally(branch: str, cwd: str) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", branch], cwd).returncode == 0


def _branch_exists_on_remote(branch: str, cwd: str) -> bool:
    return _run_git(["ls-remote", "--exit-code", "--heads", "origin", branch], cwd).returncode == 0


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

    resolved_repo_path = repo_path or CODING_REPO_PATH
    if not resolved_repo_path:
        return {"error": "repo_path not provided and CODING_REPO_PATH not configured"}
    if not os.path.isdir(resolved_repo_path):
        return {"error": f"repo_path does not exist or is not a directory: {resolved_repo_path}"}

    branch_name = f"agent/ticket-{ticket_number}"

    # Verify it's actually a git repo
    git_dir_check = _run_git(["rev-parse", "--git-dir"], resolved_repo_path)
    if git_dir_check.returncode != 0:
        log.error("instruct_implementation.not_a_git_repo", repo_path=resolved_repo_path)
        return {"error": f"{resolved_repo_path} is not a git repository"}

    # Verify working tree is clean
    status_check = _run_git(["status", "--porcelain"], resolved_repo_path)
    if status_check.returncode != 0:
        log.error("instruct_implementation.git_status_failed", stderr=status_check.stderr.strip())
        return {"error": f"git status failed: {status_check.stderr.strip()}"}
    if status_check.stdout.strip():
        log.error(
            "instruct_implementation.dirty_working_tree",
            repo_path=resolved_repo_path,
            status_output=status_check.stdout.strip()[:500],
        )
        return {
            "error": "working tree is not clean — commit or stash your changes before implementing",
            "status_output": status_check.stdout.strip(),
        }

    # Resolve configured base branch (dev by default — main is for releases)
    base_branch, detect_err = _resolve_base_branch(resolved_repo_path)
    if detect_err:
        log.error("instruct_implementation.base_branch_resolution_failed", error=detect_err)
        return {"error": detect_err}

    # Abort early if the target branch already exists anywhere
    if _branch_exists_locally(branch_name, resolved_repo_path):
        log.error("instruct_implementation.branch_exists_locally", branch_name=branch_name)
        return {"error": f"branch '{branch_name}' already exists locally — delete it or bump the ticket"}

    if _branch_exists_on_remote(branch_name, resolved_repo_path):
        log.error("instruct_implementation.branch_exists_on_remote", branch_name=branch_name)
        return {"error": f"branch '{branch_name}' already exists on origin — delete it or bump the ticket"}

    # Sync base branch
    checkout_base = _run_git(["checkout", base_branch], resolved_repo_path)
    if checkout_base.returncode != 0:
        log.error("instruct_implementation.checkout_base_failed", stderr=checkout_base.stderr.strip())
        return {"error": f"could not checkout {base_branch}: {checkout_base.stderr.strip()}"}

    pull = _run_git(["pull", "--ff-only", "origin", base_branch], resolved_repo_path)
    if pull.returncode != 0:
        log.error("instruct_implementation.pull_failed", base_branch=base_branch, stderr=pull.stderr.strip())
        return {"error": f"could not pull latest {base_branch}: {pull.stderr.strip()}"}

    # Create the working branch
    create = _run_git(["checkout", "-b", branch_name], resolved_repo_path)
    if create.returncode != 0:
        log.error("instruct_implementation.branch_create_failed", branch_name=branch_name, stderr=create.stderr.strip())
        return {"error": f"could not create branch {branch_name}: {create.stderr.strip()}"}

    log.info(
        "instruct_implementation.branch_created",
        branch_name=branch_name,
        base_branch=base_branch,
        repo_path=resolved_repo_path,
    )

    return {
        "ticket_number": ticket_number,
        "branch_name": branch_name,
        "base_branch": base_branch,
        "repo_path": resolved_repo_path,
        "stub": True,
    }
