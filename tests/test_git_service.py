"""Direct unit coverage for services/git_service.

Before this file landed, the helpers were exercised only
indirectly:

  - ``_parse_owner_repo`` / the URL regex: covered by three URL
    shape cases inside test_implementation_pr_creation.py
    (HTTPS, SSH, no .git suffix). Adding direct tests here lets
    us cover the malformed-input edge cases without burying them
    in a PR-creation test.

  - ``_run_git`` / branch existence / default-branch detection /
    base-branch resolution: not covered at all. These all shell
    out to ``git`` with specific flag combinations; a typo in
    any of those flags would silently break the implementation
    flow at production time.

The branch / default / base-resolution tests build a real git
repo via the ``tmp_git_repo`` fixture in conftest.py — these
helpers can't be meaningfully unit-tested with mocks because
their value IS the shell-out behavior.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from services.git_service import (
    _branch_exists_locally,
    _branch_exists_on_remote,
    _detect_default_branch,
    _git_origin_url,
    _parse_owner_repo,
    _resolve_base_branch,
    _run_git,
)


# ──────────────────────────────────────────────────────────────────────
# _parse_owner_repo — regex unit tests
# ──────────────────────────────────────────────────────────────────────


class TestParseOwnerRepo:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/kenruizinoue/another_coder.git", "kenruizinoue/another_coder"),
            ("git@github.com:kenruizinoue/another_coder.git", "kenruizinoue/another_coder"),
            ("https://github.com/kenruizinoue/another_coder", "kenruizinoue/another_coder"),
            ("https://github.com/kenruizinoue/another_coder/", "kenruizinoue/another_coder"),
            ("git@github.com:org/multi-word.repo-name.git", "org/multi-word.repo-name"),
        ],
    )
    def test_recognized_shapes(self, url: str, expected: str) -> None:
        assert _parse_owner_repo(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "https://gitlab.com/x/y.git",
            "https://github.com/",  # missing owner/repo segments
        ],
    )
    def test_malformed_returns_none(self, url: str) -> None:
        # Malformed / non-GitHub URLs return None — the implementation
        # router falls back to GITHUB_DEFAULT_REPO when this happens.
        # NOTE: the regex is a substring search, so a URL containing
        # ``github.com/x/y`` ANYWHERE will match. That's intentional —
        # in practice no real origin URL has GitHub embedded as a
        # path component of a different host, and the regex stays
        # simple. ``_git_origin_url`` is also gated by ``git remote
        # get-url origin``, which only returns real configured URLs.
        assert _parse_owner_repo(url) is None

    def test_strips_surrounding_whitespace(self) -> None:
        # Origin URLs from `git remote get-url` come with a trailing
        # newline. The function does its own .strip() before matching.
        assert _parse_owner_repo("  https://github.com/o/r.git\n") == "o/r"


# ──────────────────────────────────────────────────────────────────────
# _run_git + branch existence (real git, via tmp_git_repo fixture)
# ──────────────────────────────────────────────────────────────────────


class TestRunGit:
    def test_run_git_returns_completed_process(self, tmp_git_repo: Path) -> None:
        result = _run_git(["status", "--porcelain"], str(tmp_git_repo))
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        # Empty porcelain output on a clean tree.
        assert result.stdout == ""

    def test_run_git_captures_stderr_on_failure(self, tmp_git_repo: Path) -> None:
        result = _run_git(["checkout", "no-such-branch"], str(tmp_git_repo))
        assert result.returncode != 0
        # git's "did not match" / "could not match" message lives in stderr.
        assert "no-such-branch" in result.stderr or result.stderr


class TestBranchExistsLocally:
    def test_returns_true_for_existing_local_branch(self, tmp_git_repo: Path) -> None:
        # The fixture leaves us on `main` after the initial commit.
        assert _branch_exists_locally("main", str(tmp_git_repo)) is True

    def test_returns_false_for_missing_branch(self, tmp_git_repo: Path) -> None:
        assert _branch_exists_locally("nope", str(tmp_git_repo)) is False


class TestBranchExistsOnRemote:
    def test_returns_true_for_pushed_branch(self, tmp_git_repo: Path) -> None:
        # The fixture pushed `main` to the bare origin.
        assert _branch_exists_on_remote("main", str(tmp_git_repo)) is True

    def test_returns_false_for_unpushed_branch(self, tmp_git_repo: Path) -> None:
        # Create a local branch but don't push it — must not appear
        # on the remote.
        subprocess.run(
            ["git", "branch", "feature", "main"],
            cwd=str(tmp_git_repo),
            check=True,
        )
        assert _branch_exists_locally("feature", str(tmp_git_repo)) is True
        assert _branch_exists_on_remote("feature", str(tmp_git_repo)) is False


# ──────────────────────────────────────────────────────────────────────
# _git_origin_url
# ──────────────────────────────────────────────────────────────────────


class TestGitOriginUrl:
    def test_returns_origin_url_when_set(self, tmp_git_repo: Path) -> None:
        url = _git_origin_url(str(tmp_git_repo))
        assert url is not None
        # The fixture used a local bare-repo path as origin; just
        # verify it round-trips.
        assert "origin.git" in url

    def test_returns_none_when_no_origin(self, tmp_path: Path) -> None:
        # Plain dir with no .git/ at all → git command fails → None.
        empty = tmp_path / "not-a-repo"
        empty.mkdir()
        assert _git_origin_url(str(empty)) is None


# ──────────────────────────────────────────────────────────────────────
# _detect_default_branch + _resolve_base_branch
# ──────────────────────────────────────────────────────────────────────


class TestDetectDefaultBranch:
    def test_detects_main_from_bare_origin(self, tmp_git_repo: Path) -> None:
        # The fixture initializes the bare origin with --initial-branch=main.
        # ls-remote --symref origin HEAD should return refs/heads/main.
        assert _detect_default_branch(str(tmp_git_repo)) == "main"

    def test_returns_none_when_remote_unreachable(self, tmp_path: Path) -> None:
        # No origin remote at all — ls-remote will fail. Function
        # must return None rather than raising.
        empty = tmp_path / "no-origin"
        empty.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=str(empty),
            check=True,
            capture_output=True,
        )
        assert _detect_default_branch(str(empty)) is None


class TestResolveBaseBranch:
    def test_override_wins_when_present_on_remote(self, tmp_git_repo: Path) -> None:
        # Push a `dev` branch to origin so the override exists.
        subprocess.run(
            ["git", "branch", "dev", "main"],
            cwd=str(tmp_git_repo),
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "dev"],
            cwd=str(tmp_git_repo),
            check=True,
            capture_output=True,
        )
        branch, err = _resolve_base_branch(str(tmp_git_repo), override="dev")
        assert err is None
        assert branch == "dev"

    def test_override_missing_on_remote_returns_error(
        self, tmp_git_repo: Path
    ) -> None:
        # Operator passed a branch the remote doesn't have. The
        # error must surface — silently falling back would create a
        # PR against the wrong base branch.
        branch, err = _resolve_base_branch(
            str(tmp_git_repo), override="never-pushed"
        )
        assert branch is None
        assert err is not None
        assert "never-pushed" in err

    def test_no_override_uses_detected_default(
        self, tmp_git_repo: Path
    ) -> None:
        # No override → falls through to _detect_default_branch which
        # reads HEAD's symref from the remote. The fixture's HEAD is
        # main.
        branch, err = _resolve_base_branch(str(tmp_git_repo))
        assert err is None
        assert branch == "main"

    def test_falls_back_to_base_branch_env_when_detection_fails(
        self, tmp_git_repo: Path
    ) -> None:
        # Patch _detect_default_branch to fail so we exercise the
        # BASE_BRANCH fallback. Push a `dev` branch first so the
        # fallback finds it on the remote.
        subprocess.run(
            ["git", "branch", "dev", "main"],
            cwd=str(tmp_git_repo),
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "dev"],
            cwd=str(tmp_git_repo),
            check=True,
            capture_output=True,
        )

        with patch(
            "services.git_service._detect_default_branch", return_value=None
        ), patch("services.git_service.BASE_BRANCH", "dev"):
            branch, err = _resolve_base_branch(str(tmp_git_repo))

        assert err is None
        assert branch == "dev"

    def test_no_detection_no_env_returns_error(self, tmp_git_repo: Path) -> None:
        # Both auto-detection and BASE_BRANCH fallback unavailable —
        # function bails with a clear error rather than guessing.
        with patch(
            "services.git_service._detect_default_branch", return_value=None
        ), patch("services.git_service.BASE_BRANCH", ""):
            branch, err = _resolve_base_branch(str(tmp_git_repo))

        assert branch is None
        assert err is not None
        assert "default branch" in err.lower()
