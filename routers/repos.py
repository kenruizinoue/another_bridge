"""Workspace repository discovery + path-validation guard.

Exposes:
- POST /tools/list_repos: webhook tool returning repos under WORKSPACE_ROOT
  that have a .git/ directory AND a configured origin remote. Filters out
  half-cloned forks, backups, and other non-repo directories.
- validate_repo_path(): shared helper used by planning + implementation
  routers to reject repo_path values outside the workspace root, including
  symlink-escape and ../ traversal attempts.

When WORKSPACE_ROOT is unset, validate_repo_path falls back to the legacy
"is it a directory?" check so existing CODING_REPO_PATH deployments keep
working unchanged.

Git URL parsing + origin lookup live in services.git_service. Re-exported
here as `_git_origin_url` / `_parse_owner_repo` so callers that imported
them from this module continue to work.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from fastapi import APIRouter, Request

from config import WORKSPACE_ROOT
from services.git_service import _git_origin_url, _parse_owner_repo
from services.request import extract_args

# Back-compat re-exports — older code imports these from routers.repos.
__all__ = [
    "router",
    "validate_repo_path",
    "list_workspace_repos",
    "_git_origin_url",
    "_parse_owner_repo",
]

log = structlog.get_logger()

router = APIRouter()


def list_workspace_repos(workspace_root: str) -> list[dict[str, Any]]:
    """Walk one level deep, return git repos with an origin remote."""
    if not workspace_root or not os.path.isdir(workspace_root):
        return []

    repos: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(workspace_root)):
        repo_dir = os.path.join(workspace_root, entry)
        if not os.path.isdir(repo_dir):
            continue
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            continue
        origin_url = _git_origin_url(repo_dir)
        if not origin_url:
            # Local-only repo without an origin — likely scratch/backup.
            continue
        repos.append(
            {
                "name": entry,
                "path": repo_dir,
                "origin_url": origin_url,
                "owner_repo": _parse_owner_repo(origin_url),
            }
        )
    return repos


def validate_repo_path(
    repo_path: str,
    workspace_root: str | None = None,
    require_git_repo: bool = True,
) -> tuple[str | None, str | None]:
    """Validate repo_path and return (resolved_path, error_message).

    On success: (resolved_absolute_path, None).
    On failure: (None, error_message_for_caller).

    When workspace_root is unset, only checks isdir — preserves the
    pre-allow-list behavior so CODING_REPO_PATH deployments aren't broken
    by upgrading.

    workspace_root resolution: when the caller passes None (the common
    case from production code), we read the module-level WORKSPACE_ROOT
    AT CALL TIME rather than at function-definition time. The previous
    ``= WORKSPACE_ROOT`` default arg evaluated once at import, which
    made it impossible for tests (and runtime config reloads) to
    monkey-patch the env without re-importing the module.

    require_git_repo (default True): when True (planning + implementation
    flows) the candidate must be a STRICT subdirectory of workspace_root
    AND contain a ``.git/`` dir — those flows commit, push, and open PRs
    so a non-repo path would fail later anyway. When False (the chat
    flow) the candidate is allowed to BE the workspace root itself, and
    the .git/ check is skipped — chat doesn't commit, and a workspace-
    root cwd lets Claude roam across repos for cross-cutting questions.
    The path-bound (must be inside workspace_root) is preserved either
    way; it's the security floor.
    """
    if workspace_root is None:
        workspace_root = WORKSPACE_ROOT
    if not isinstance(repo_path, str) or not repo_path.strip():
        return None, "repo_path must be a non-empty string"

    candidate = os.path.realpath(os.path.expanduser(repo_path))
    if not os.path.isdir(candidate):
        return None, f"repo_path does not exist or is not a directory: {repo_path}"

    if not workspace_root:
        return candidate, None

    workspace_resolved = os.path.realpath(os.path.expanduser(workspace_root))

    try:
        common = os.path.commonpath([workspace_resolved, candidate])
    except ValueError:
        return None, f"repo_path is outside the workspace root: {repo_path}"

    if common != workspace_resolved:
        return (
            None,
            f"repo_path must be a directory inside {workspace_root}, got: {repo_path}",
        )

    if require_git_repo:
        if candidate == workspace_resolved:
            return (
                None,
                f"repo_path must be a directory inside {workspace_root}, got: {repo_path}",
            )
        if not os.path.isdir(os.path.join(candidate, ".git")):
            return None, f"repo_path is not a git repository (no .git/ found): {repo_path}"

    return candidate, None


@router.post("/tools/list_repos")
async def list_repos(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    _ = extract_args(body)  # no args today; reserved for future filters

    log.info("list_repos.request", workspace_root=WORKSPACE_ROOT)

    if not WORKSPACE_ROOT:
        return {
            "error": (
                "WORKSPACE_ROOT not configured in another_bridge/.env. "
                "Set WORKSPACE_ROOT to the parent directory containing your "
                "git repos so the agent can enumerate and target them."
            )
        }

    if not os.path.isdir(WORKSPACE_ROOT):
        return {
            "error": (
                f"WORKSPACE_ROOT is set but does not exist or is not a "
                f"directory: {WORKSPACE_ROOT}"
            )
        }

    repos = list_workspace_repos(WORKSPACE_ROOT)
    log.info("list_repos.response", count=len(repos))
    return {
        "workspace_root": WORKSPACE_ROOT,
        "count": len(repos),
        "repos": repos,
        # Cross-turn structured state — the platform strips __context__
        # from the LLM-visible body and persists each key as a context
        # artifact on the assistant message. The next turn's prompt then
        # surfaces `available_repos` in its [Conversation context] block,
        # so the Planner can read repo_path / owner_repo structurally
        # instead of relying on the EM to relay markdown verbatim (which
        # gpt-4o-mini routinely fails to do — it rewrites bullet lists
        # as hyperlinks and drops the absolute path inside backticks).
        "__context__": {
            "available_repos": repos,
        },
    }
