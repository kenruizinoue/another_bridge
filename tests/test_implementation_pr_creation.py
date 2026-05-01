"""Tests for the PR-creation logic in routers/implementation.py.

Covers two extracted helpers:
  - resolve_pr_repo_slug — derives the GitHub owner/repo for the PR API call
    from the local clone's origin remote, falling back to GITHUB_DEFAULT_REPO.
  - format_pr_creation_error — formats the error message returned to the
    user, with enrichment for the 422 head:invalid case.

Regression context: when implementing tickets across multiple repos in
WORKSPACE_ROOT, the PR creation API was hardcoded to use
GITHUB_DEFAULT_REPO, which produced "GitHub PR API 422: head invalid"
because the branch was pushed to a different repo (the one matching the
local clone's origin) than the PR was being requested at. These tests
lock down the fix so that regression doesn't return.
"""

from unittest.mock import patch

import pytest

from routers.implementation import (
    format_pr_creation_error,
    resolve_pr_repo_slug,
)


# ──────────────────────────────────────────────────────────────────────
# resolve_pr_repo_slug
# ──────────────────────────────────────────────────────────────────────


class TestResolvePrRepoSlug:
    """Verifies the precedence: local origin → GITHUB_DEFAULT_REPO → None."""

    def test_derives_slug_from_local_origin_https_url(self):
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://github.com/kenruizinoue/lunchy_box_frontend.git",
        ):
            result = resolve_pr_repo_slug(
                "/path/to/repo", github_default_repo="kenruizinoue/some_other_repo"
            )
        assert result == "kenruizinoue/lunchy_box_frontend"

    def test_derives_slug_from_local_origin_ssh_url(self):
        with patch(
            "routers.implementation._git_origin_url",
            return_value="git@github.com:kenruizinoue/lunchy_box_frontend.git",
        ):
            result = resolve_pr_repo_slug(
                "/path/to/repo", github_default_repo="other"
            )
        assert result == "kenruizinoue/lunchy_box_frontend"

    def test_derives_slug_without_dot_git_suffix(self):
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://github.com/kenruizinoue/repo_name",
        ):
            result = resolve_pr_repo_slug(
                "/path/to/repo", github_default_repo="other"
            )
        assert result == "kenruizinoue/repo_name"

    def test_local_origin_overrides_github_default_repo(self):
        """The actual regression case: GITHUB_DEFAULT_REPO points to one
        repo (e.g. another_agent_frontend) but the user is implementing
        in another (e.g. lunchy_box_frontend). PR creation MUST follow
        the local origin, not the env default."""
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://github.com/kenruizinoue/lunchy_box_frontend.git",
        ):
            result = resolve_pr_repo_slug(
                "/path/to/lunchy_box_frontend",
                github_default_repo="kenruizinoue/another_agent_frontend",
            )
        assert result == "kenruizinoue/lunchy_box_frontend", (
            "PR slug must be derived from local origin, not GITHUB_DEFAULT_REPO. "
            "Otherwise multi-repo workspaces hit '422 head invalid'."
        )

    def test_falls_back_to_github_default_repo_when_no_origin(self):
        with patch("routers.implementation._git_origin_url", return_value=None):
            result = resolve_pr_repo_slug(
                "/path/to/repo", github_default_repo="kenruizinoue/fallback"
            )
        assert result == "kenruizinoue/fallback"

    def test_falls_back_to_github_default_when_origin_is_non_github(self):
        """Local origin pointing at GitLab / Bitbucket / private host should
        not produce a slug — fall back to env default."""
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://gitlab.com/owner/repo.git",
        ):
            result = resolve_pr_repo_slug(
                "/path/to/repo", github_default_repo="kenruizinoue/fallback"
            )
        assert result == "kenruizinoue/fallback"

    def test_returns_none_when_both_origin_and_default_unavailable(self):
        with patch("routers.implementation._git_origin_url", return_value=None):
            result = resolve_pr_repo_slug("/path/to/repo", github_default_repo="")
        assert result is None

    def test_returns_none_when_origin_unparseable_and_default_empty(self):
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://gitlab.com/owner/repo.git",
        ):
            result = resolve_pr_repo_slug("/path/to/repo", github_default_repo="")
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# format_pr_creation_error
# ──────────────────────────────────────────────────────────────────────


# A real 422 head:invalid response shape (fields the detection looks for).
HEAD_INVALID_BODY = (
    '{"message":"Validation Failed","errors":'
    '[{"resource":"PullRequest","field":"head","code":"invalid"}],'
    '"documentation_url":"https://docs.github.com/rest/pulls/pulls",'
    '"status":"422"}'
)


class TestFormatPrCreationError:
    """Verifies the 422 head:invalid enrichment vs. verbatim passthrough
    for everything else."""

    def test_422_head_invalid_includes_diagnose_command(self):
        out = format_pr_creation_error(
            status_code=422,
            response_text=HEAD_INVALID_BODY,
            branch_name="agent/ticket-10",
            repo_slug="kenruizinoue/lunchy_box_frontend",
            base_branch="main",
            repo_path="/Users/kenruizinoue/Desktop/AnohterAgent Projects/lunchy_box_frontend",
        )
        assert "git remote -v" in out
        assert "/Users/kenruizinoue/Desktop/AnohterAgent Projects/lunchy_box_frontend" in out

    def test_422_head_invalid_includes_fix_command(self):
        out = format_pr_creation_error(
            status_code=422,
            response_text=HEAD_INVALID_BODY,
            branch_name="agent/ticket-10",
            repo_slug="kenruizinoue/lunchy_box_frontend",
            base_branch="main",
            repo_path="/Users/x/repo",
        )
        assert "git remote set-url origin" in out
        assert "https://github.com/<correct-owner>/<correct-repo>.git" in out

    def test_422_head_invalid_includes_manual_pr_url(self):
        """User can open the PR by hand instead of debugging — surface the URL."""
        out = format_pr_creation_error(
            status_code=422,
            response_text=HEAD_INVALID_BODY,
            branch_name="agent/ticket-10",
            repo_slug="kenruizinoue/lunchy_box_frontend",
            base_branch="main",
            repo_path="/Users/x/repo",
        )
        assert (
            "https://github.com/kenruizinoue/lunchy_box_frontend/compare/main...agent/ticket-10"
            in out
        )

    def test_422_head_invalid_preserves_original_response(self):
        out = format_pr_creation_error(
            status_code=422,
            response_text=HEAD_INVALID_BODY,
            branch_name="agent/ticket-10",
            repo_slug="kenruizinoue/lunchy_box_frontend",
            base_branch="main",
            repo_path="/Users/x/repo",
        )
        assert HEAD_INVALID_BODY in out

    def test_422_head_invalid_mentions_branch_and_repo(self):
        out = format_pr_creation_error(
            status_code=422,
            response_text=HEAD_INVALID_BODY,
            branch_name="agent/ticket-99",
            repo_slug="owner/some_repo",
            base_branch="main",
            repo_path="/p",
        )
        assert "agent/ticket-99" in out
        assert "owner/some_repo" in out

    def test_non_422_passes_through_verbatim(self):
        """500, 503, network errors etc. — no enrichment, just relay."""
        body = '{"message":"Internal Server Error"}'
        out = format_pr_creation_error(
            status_code=500,
            response_text=body,
            branch_name="agent/ticket-10",
            repo_slug="owner/repo",
            base_branch="main",
            repo_path="/p",
        )
        assert out == f"GitHub PR API 500: {body}"
        assert "git remote set-url" not in out

    def test_422_with_different_field_passes_through(self):
        """422s on other fields (e.g. base) shouldn't trigger head-specific advice."""
        body = (
            '{"message":"Validation Failed","errors":'
            '[{"resource":"PullRequest","field":"base","code":"invalid"}]}'
        )
        out = format_pr_creation_error(
            status_code=422,
            response_text=body,
            branch_name="agent/ticket-10",
            repo_slug="owner/repo",
            base_branch="nonexistent",
            repo_path="/p",
        )
        assert out == f"GitHub PR API 422: {body}"
        assert "git remote set-url" not in out

    def test_422_with_head_field_but_different_code_passes_through(self):
        """422 saying head is e.g. 'missing' rather than 'invalid'."""
        body = (
            '{"message":"Validation Failed","errors":'
            '[{"resource":"PullRequest","field":"head","code":"missing"}]}'
        )
        out = format_pr_creation_error(
            status_code=422,
            response_text=body,
            branch_name="agent/ticket-10",
            repo_slug="owner/repo",
            base_branch="main",
            repo_path="/p",
        )
        assert out == f"GitHub PR API 422: {body}"
        assert "git remote set-url" not in out

    def test_4xx_other_passes_through(self):
        out = format_pr_creation_error(
            status_code=404,
            response_text="Not Found",
            branch_name="agent/ticket-10",
            repo_slug="owner/repo",
            base_branch="main",
            repo_path="/p",
        )
        assert out == "GitHub PR API 404: Not Found"


# ──────────────────────────────────────────────────────────────────────
# Integration-shaped: regression scenario end-to-end (helpers composed)
# ──────────────────────────────────────────────────────────────────────


class TestRegressionScenario:
    """The exact failure mode that motivated this work: GITHUB_DEFAULT_REPO
    points to A, user implements in B (different local clone), PR creation
    correctly targets B (not A) thanks to resolve_pr_repo_slug."""

    def test_multi_repo_workspace_targets_local_origin_not_env_default(self):
        with patch(
            "routers.implementation._git_origin_url",
            return_value="https://github.com/kenruizinoue/lunchy_box_frontend.git",
        ):
            slug = resolve_pr_repo_slug(
                "/Users/k/AnohterAgent Projects/lunchy_box_frontend",
                github_default_repo="kenruizinoue/another_agent_frontend",
            )
        # PR API call would use this slug — must match where the branch
        # was actually pushed (the local origin), not the env default.
        assert slug == "kenruizinoue/lunchy_box_frontend"
        assert slug != "kenruizinoue/another_agent_frontend"
