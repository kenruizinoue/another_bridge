"""Direct unit coverage for services/github_service.

Before this file landed, github_service was only exercised
indirectly — through test_implementation_pr_creation (which
covers the format_pr_creation_error helper + a few resolve_pr_repo_-
slug edge cases via the implementation router) and through the
list_repos / list_workspace_repos paths. The actual API client
functions (search_issues, get_issue, create_pr) had no direct
tests, which left:

  - The PR-filtering branch in search_issues unverified — a
    regression that stops filtering ``pull_request`` items from
    the issues endpoint would silently include PRs in plan
    results.

  - The 422-when-pull-request guard in get_issue unverified —
    the planner depends on this to refuse "synthesize plan for
    a merged PR" requests.

  - The auth-header + JSON-body shape on create_pr unverified —
    a typo in the headers dict would surface as 401s in
    production but never trip a test.

These tests mock ``services.github_service.requests`` rather than
issuing real HTTP. The mock pattern matches what the rest of the
suite uses (subprocess.Popen patched at the seam) — keeps the
test self-contained and fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import github_service


# ──────────────────────────────────────────────────────────────────────
# Helpers — tiny stub of requests.Response for the mock to return.
# ──────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, json_body=None, text: str = "") -> None:
        self.status_code = status
        self._json = json_body
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


# ──────────────────────────────────────────────────────────────────────
# search_issues
# ──────────────────────────────────────────────────────────────────────


class TestSearchIssues:
    def test_happy_path_returns_normalized_shape(self) -> None:
        # GitHub's /repos/<owner>/<repo>/issues returns a list of
        # issue objects; we project to a smaller shape that includes
        # number, title, labels, url, state. Lock the projection so a
        # future change that drops a field gets caught.
        github_response = [
            {
                "number": 42,
                "title": "Fix the bug",
                "labels": [{"name": "bug"}, {"name": "p1"}],
                "html_url": "https://github.com/o/r/issues/42",
                "state": "open",
            }
        ]
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(200, github_response)
            result, err = github_service.search_issues("o/r", label="bug", state="open")

        assert err is None
        assert result is not None
        assert result["count"] == 1
        assert result["repo"] == "o/r"
        assert result["issues"] == [
            {
                "number": 42,
                "title": "Fix the bug",
                "labels": ["bug", "p1"],
                "url": "https://github.com/o/r/issues/42",
                "state": "open",
            }
        ]

    def test_filters_pull_requests_out_of_results(self) -> None:
        # CRITICAL: GitHub's issues endpoint returns BOTH issues and
        # pull requests in the same list. PRs have a "pull_request"
        # key that issues don't. The filter at services/github_service.py
        # is what keeps PRs out of plan-target lists. Lock it down.
        github_response = [
            {
                "number": 1,
                "title": "Real issue",
                "labels": [],
                "html_url": "https://github.com/o/r/issues/1",
                "state": "open",
            },
            {
                "number": 2,
                "title": "Pretending to be an issue",
                "pull_request": {"url": "..."},  # ← the discriminator
                "labels": [],
                "html_url": "https://github.com/o/r/pull/2",
                "state": "open",
            },
        ]
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(200, github_response)
            result, err = github_service.search_issues("o/r", None, "open")

        assert err is None
        assert result is not None
        assert result["count"] == 1
        assert [i["number"] for i in result["issues"]] == [1]

    def test_non_ok_response_returns_error(self) -> None:
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(403, text="rate limit")
            result, err = github_service.search_issues("o/r", None, "open")

        assert result is None
        assert err is not None
        assert "403" in err
        assert "rate limit" in err

    def test_label_param_passed_through_when_set(self) -> None:
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(200, [])
            github_service.search_issues("o/r", label="bug", state="closed")

        # The first positional arg is the URL; params is a kwarg.
        call_kwargs = mock_req.get.call_args.kwargs
        assert call_kwargs["params"]["labels"] == "bug"
        assert call_kwargs["params"]["state"] == "closed"

    def test_empty_label_is_omitted_from_params(self) -> None:
        # Whitespace-only / empty label means "any label" — must NOT
        # be sent to GitHub or it would interpret as "label="" which
        # returns nothing.
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(200, [])
            github_service.search_issues("o/r", label="   ", state="open")

        params = mock_req.get.call_args.kwargs["params"]
        assert "labels" not in params


# ──────────────────────────────────────────────────────────────────────
# get_issue
# ──────────────────────────────────────────────────────────────────────


class TestGetIssue:
    def _setup(self, issue_status: int, issue_body, comments_status: int = 200, comments_body=None):
        """Set up the two sequential GET calls — issue + comments."""
        responses = [
            _FakeResponse(issue_status, issue_body),
            _FakeResponse(comments_status, comments_body or []),
        ]
        return responses

    def test_happy_path_with_comments(self) -> None:
        issue_body = {
            "number": 7,
            "title": "Add login",
            "body": "Make it work.",
            "labels": [{"name": "feature"}],
            "html_url": "https://github.com/o/r/issues/7",
        }
        comments_body = [
            {
                "user": {"login": "alice"},
                "body": "looks good",
                "created_at": "2026-04-30T12:00:00Z",
            }
        ]
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.side_effect = self._setup(200, issue_body, 200, comments_body)
            result, err = github_service.get_issue("o/r", 7)

        assert err is None
        assert result is not None
        assert result["number"] == 7
        assert result["title"] == "Add login"
        assert result["body"] == "Make it work."
        assert result["labels"] == ["feature"]
        assert len(result["comments"]) == 1
        assert result["comments"][0]["author"] == "alice"

    def test_pull_request_returns_error(self) -> None:
        # The "is a PR not an issue" guard. The planner relies on this
        # to refuse synthesize-plan-for-merged-PR requests, which would
        # otherwise produce nonsense plans referencing already-merged
        # diffs.
        pr_body = {
            "number": 9,
            "title": "PR not issue",
            "body": "...",
            "pull_request": {"url": "..."},
            "labels": [],
            "html_url": "https://github.com/o/r/pull/9",
        }
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(200, pr_body)
            result, err = github_service.get_issue("o/r", 9)

        assert result is None
        assert err is not None
        assert "pull request" in err.lower()
        assert "#9" in err

    def test_404_propagates_as_error(self) -> None:
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.return_value = _FakeResponse(404, text="Not Found")
            result, err = github_service.get_issue("o/r", 999)

        assert result is None
        assert err is not None
        assert "404" in err

    def test_empty_body_normalized_to_empty_string(self) -> None:
        # GitHub returns {"body": null} for issues with no body. The
        # planning prompt does ``ticket_body.strip()`` so a None would
        # crash. Coerce here defensively.
        issue_body = {
            "number": 1,
            "title": "Empty body",
            "body": None,
            "labels": [],
            "html_url": "https://github.com/o/r/issues/1",
        }
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.side_effect = self._setup(200, issue_body, 200, [])
            result, _ = github_service.get_issue("o/r", 1)

        assert result is not None
        assert result["body"] == ""

    def test_comments_failure_surfaces_as_error(self) -> None:
        # Issue fetch succeeds but comments fetch fails. The function
        # bails — the planner needs comments to understand the user's
        # context, so a partial result would be misleading.
        issue_body = {
            "number": 1,
            "title": "x",
            "body": "y",
            "labels": [],
            "html_url": "...",
        }
        with patch.object(github_service, "requests") as mock_req:
            mock_req.get.side_effect = [
                _FakeResponse(200, issue_body),
                _FakeResponse(503, text="upstream down"),
            ]
            result, err = github_service.get_issue("o/r", 1)

        assert result is None
        assert err is not None
        assert "503" in err


# ──────────────────────────────────────────────────────────────────────
# create_pr
# ──────────────────────────────────────────────────────────────────────


class TestCreatePr:
    def test_builds_correct_url_and_body(self) -> None:
        with patch.object(github_service, "requests") as mock_req:
            mock_req.post.return_value = _FakeResponse(
                201, {"html_url": "https://github.com/o/r/pull/1", "number": 1}
            )
            github_service.create_pr(
                repo_slug="o/r",
                title="Implement #5",
                body="See plan.",
                head="agent/ticket-5",
                base="dev",
            )

        # First positional arg is the URL — should hit /repos/<slug>/pulls.
        call_args = mock_req.post.call_args
        url = call_args.args[0]
        assert url.endswith("/repos/o/r/pulls")
        # JSON body has all four fields.
        body = call_args.kwargs["json"]
        assert body == {
            "title": "Implement #5",
            "body": "See plan.",
            "head": "agent/ticket-5",
            "base": "dev",
        }

    def test_sends_auth_headers(self) -> None:
        # Auth header typo here would silently 401 in production.
        # Lock the three keys: Authorization (Bearer), Accept, and
        # X-GitHub-Api-Version.
        with patch.object(github_service, "requests") as mock_req:
            mock_req.post.return_value = _FakeResponse(201, {})
            github_service.create_pr(
                repo_slug="o/r",
                title="t",
                body="b",
                head="h",
                base="m",
            )

        headers = mock_req.post.call_args.kwargs["headers"]
        # The exact PAT depends on GITHUB_PAT env at test time, so just
        # verify the prefix shape.
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"

    def test_returns_raw_response_for_caller_inspection(self) -> None:
        # create_pr deliberately returns the raw response so the
        # caller can inspect status_code + text via
        # format_pr_creation_error. Verify the contract.
        canned = _FakeResponse(422, text='{"message":"head invalid"}')
        with patch.object(github_service, "requests") as mock_req:
            mock_req.post.return_value = canned
            resp = github_service.create_pr("o/r", "t", "b", "h", "m")

        assert resp is canned
        assert resp.status_code == 422
