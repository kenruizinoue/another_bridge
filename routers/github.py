from typing import Any

import structlog
from fastapi import APIRouter, Request

from config import GITHUB_DEFAULT_REPO, GITHUB_PAT
from services import github_service
from services.request import extract_args

log = structlog.get_logger()

router = APIRouter()


@router.post("/tools/github_search_issues")
async def github_search_issues(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = extract_args(body)
    repo = args.get("repo") or GITHUB_DEFAULT_REPO
    label = args.get("label")
    state = args.get("state") or "open"

    log.info("github_search_issues.request", repo=repo, label=label, state=state)

    if not GITHUB_PAT:
        log.error("github_search_issues.missing_pat")
        return {"error": "GITHUB_PAT not configured in another_bridge/.env"}
    if not repo:
        log.error("github_search_issues.missing_repo")
        return {"error": "repo not provided and GITHUB_DEFAULT_REPO not configured"}

    result, err = github_service.search_issues(repo=repo, label=label, state=state)
    if err is not None:
        log.error("github_search_issues.api_failed", error=err)
        return {"error": err}

    log.info("github_search_issues.ok", count=result["count"], repo=repo)
    return result


@router.post("/tools/github_get_issue")
async def github_get_issue(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = extract_args(body)
    repo = args.get("repo") or GITHUB_DEFAULT_REPO
    issue_number_raw = args.get("issue_number")

    log.info("github_get_issue.request", repo=repo, issue_number=issue_number_raw)

    if not GITHUB_PAT:
        log.error("github_get_issue.missing_pat")
        return {"error": "GITHUB_PAT not configured in another_bridge/.env"}
    if not repo:
        log.error("github_get_issue.missing_repo")
        return {"error": "repo not provided and GITHUB_DEFAULT_REPO not configured"}

    try:
        issue_number = int(issue_number_raw)
    except (TypeError, ValueError):
        log.error("github_get_issue.invalid_issue_number", value=issue_number_raw)
        return {"error": "issue_number is required and must be an integer"}

    result, err = github_service.get_issue(repo=repo, issue_number=issue_number)
    if err is not None:
        log.error("github_get_issue.api_failed", error=err)
        return {"error": err}

    log.info("github_get_issue.ok", issue_number=issue_number, comment_count=len(result["comments"]))
    return result
