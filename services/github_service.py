"""GitHub REST API + PR-creation helpers.

Consolidates what used to live across routers/github.py (issue search,
issue fetch with comments) and routers/implementation.py (PR creation +
the multi-repo slug resolver + 422-head-invalid error formatter).

The PR helpers (resolve_pr_repo_slug, format_pr_creation_error) keep
their original signatures so existing tests in test_implementation_pr_creation
patch them via routers.implementation re-exports unchanged.
"""

from __future__ import annotations

from typing import Any

import requests

from config import GITHUB_API_BASE, GITHUB_PAT


GITHUB_TIMEOUT_SECONDS = 15
PR_CREATE_TIMEOUT_SECONDS = 30


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def search_issues(repo: str, label: str | None, state: str) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (response_dict, error_message). On success, response_dict
    has shape {"issues": [...], "count": int, "repo": str}. On failure,
    returns (None, error_message). Pulls request items are filtered out
    — the search endpoint returns both issues and PRs in the same list."""
    params: dict[str, Any] = {"state": state, "per_page": 100}
    if isinstance(label, str) and label.strip():
        params["labels"] = label.strip()

    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues",
        headers=_auth_headers(),
        params=params,
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        return None, f"GitHub API {resp.status_code}: {resp.text}"

    issues = [
        {
            "number": item["number"],
            "title": item["title"],
            "labels": [lbl["name"] for lbl in item.get("labels", [])],
            "url": item["html_url"],
            "state": item["state"],
        }
        for item in resp.json()
        if "pull_request" not in item
    ]
    return {"issues": issues, "count": len(issues), "repo": repo}, None


def get_issue(repo: str, issue_number: int) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (issue_dict, error_message). issue_dict has shape
    {number, title, body, labels, comments, url}. Refuses PRs (returns
    error) since the planning prompt expects an issue body."""
    issue_resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}",
        headers=_auth_headers(),
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if not issue_resp.ok:
        return None, f"GitHub API {issue_resp.status_code}: {issue_resp.text}"

    issue = issue_resp.json()
    if "pull_request" in issue:
        return None, f"#{issue_number} is a pull request, not an issue"

    comments_resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments",
        headers=_auth_headers(),
        params={"per_page": 100},
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if not comments_resp.ok:
        return None, f"GitHub comments API {comments_resp.status_code}: {comments_resp.text}"

    comments = [
        {
            "author": c.get("user", {}).get("login"),
            "body": c.get("body", ""),
            "created_at": c.get("created_at"),
        }
        for c in comments_resp.json()
    ]

    return (
        {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "labels": [lbl["name"] for lbl in issue.get("labels", [])],
            "comments": comments,
            "url": issue["html_url"],
        },
        None,
    )


def create_pr(
    repo_slug: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> requests.Response:
    """POST /repos/<slug>/pulls. Returns the raw response so the caller
    can inspect status/text via format_pr_creation_error on failure."""
    return requests.post(
        f"{GITHUB_API_BASE}/repos/{repo_slug}/pulls",
        headers=_auth_headers(),
        json={"title": title, "body": body, "head": head, "base": base},
        timeout=PR_CREATE_TIMEOUT_SECONDS,
    )


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
