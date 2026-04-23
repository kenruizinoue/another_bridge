from typing import Any

import requests
import structlog
from fastapi import APIRouter, Request

from config import GITHUB_API_BASE, GITHUB_DEFAULT_REPO, GITHUB_PAT

log = structlog.get_logger()

router = APIRouter()


def _extract_args(body: Any) -> dict[str, Any]:
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}
    return args


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@router.post("/tools/github_search_issues")
async def github_search_issues(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = _extract_args(body)
    repo = args.get("repo") or GITHUB_DEFAULT_REPO
    label = args.get("label")
    state = args.get("state") or "open"

    log.info("github_search_issues.request", repo=repo, label=label, state=state)

    if not GITHUB_PAT:
        log.error("github_search_issues.missing_pat")
        return {"error": "GITHUB_PAT not configured in another_coder/.env"}
    if not repo:
        log.error("github_search_issues.missing_repo")
        return {"error": "repo not provided and GITHUB_DEFAULT_REPO not configured"}

    params: dict[str, Any] = {"state": state, "per_page": 30}
    if isinstance(label, str) and label.strip():
        params["labels"] = label.strip()

    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues",
        headers=_auth_headers(),
        params=params,
        timeout=15,
    )
    if not resp.ok:
        log.error("github_search_issues.api_failed", status=resp.status_code, body=resp.text)
        return {"error": f"GitHub API {resp.status_code}: {resp.text}"}

    raw = resp.json()
    issues = [
        {
            "number": item["number"],
            "title": item["title"],
            "labels": [lbl["name"] for lbl in item.get("labels", [])],
            "url": item["html_url"],
            "state": item["state"],
        }
        for item in raw
        if "pull_request" not in item
    ]

    log.info("github_search_issues.ok", count=len(issues), repo=repo)
    return {"issues": issues, "count": len(issues), "repo": repo}


@router.post("/tools/github_get_issue")
async def github_get_issue(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = _extract_args(body)
    repo = args.get("repo") or GITHUB_DEFAULT_REPO
    issue_number_raw = args.get("issue_number")

    log.info("github_get_issue.request", repo=repo, issue_number=issue_number_raw)

    if not GITHUB_PAT:
        log.error("github_get_issue.missing_pat")
        return {"error": "GITHUB_PAT not configured in another_coder/.env"}
    if not repo:
        log.error("github_get_issue.missing_repo")
        return {"error": "repo not provided and GITHUB_DEFAULT_REPO not configured"}

    try:
        issue_number = int(issue_number_raw)
    except (TypeError, ValueError):
        log.error("github_get_issue.invalid_issue_number", value=issue_number_raw)
        return {"error": "issue_number is required and must be an integer"}

    issue_resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}",
        headers=_auth_headers(),
        timeout=15,
    )
    if not issue_resp.ok:
        log.error("github_get_issue.api_failed", status=issue_resp.status_code, body=issue_resp.text)
        return {"error": f"GitHub API {issue_resp.status_code}: {issue_resp.text}"}

    issue = issue_resp.json()
    if "pull_request" in issue:
        log.error("github_get_issue.is_pull_request", issue_number=issue_number)
        return {"error": f"#{issue_number} is a pull request, not an issue"}

    comments_resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments",
        headers=_auth_headers(),
        params={"per_page": 100},
        timeout=15,
    )
    if not comments_resp.ok:
        log.error("github_get_issue.comments_failed", status=comments_resp.status_code, body=comments_resp.text)
        return {"error": f"GitHub comments API {comments_resp.status_code}: {comments_resp.text}"}

    comments = [
        {
            "author": c.get("user", {}).get("login"),
            "body": c.get("body", ""),
            "created_at": c.get("created_at"),
        }
        for c in comments_resp.json()
    ]

    log.info("github_get_issue.ok", issue_number=issue_number, comment_count=len(comments))
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue.get("body") or "",
        "labels": [lbl["name"] for lbl in issue.get("labels", [])],
        "comments": comments,
        "url": issue["html_url"],
    }
