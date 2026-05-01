"""Git command helpers used by planning + implementation.

Consolidates what used to live across routers/implementation.py (subprocess
runners, branch existence checks, default-branch detection, base-branch
resolution) and routers/repos.py (origin-url + GitHub-slug parsing) so
all `git`-shaped operations live in one place.

Public helpers keep their underscore prefix because existing tests patch
them by these names — re-exporting them at routers.implementation /
routers.repos preserves the patch points without forcing test rewrites.
"""

from __future__ import annotations

import re
import subprocess

from config import BASE_BRANCH


GIT_TIMEOUT_SECONDS = 60


# Matches owner/repo at the end of a GitHub HTTPS or SSH URL, with or
# without a trailing .git. Examples it parses:
#   https://github.com/kenruizinoue/another_agent_frontend.git
#   git@github.com:kenruizinoue/another_agent_frontend.git
#   https://github.com/kenruizinoue/another_agent_frontend
_GITHUB_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


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


def _parse_owner_repo(origin_url: str) -> str | None:
    m = _GITHUB_URL_RE.search(origin_url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _git_origin_url(repo_dir: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


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
        if line.startswith("ref:") and "refs/heads/" in line:
            try:
                ref_part = line.split("ref:", 1)[1].strip().split("\t", 1)[0].strip()
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
    if override:
        if not _branch_exists_on_remote(override, cwd):
            return None, f"requested base_branch '{override}' does not exist on origin"
        return override, None

    detected = _detect_default_branch(cwd)
    if detected:
        return detected, None

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
