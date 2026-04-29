"""Tests for the repo_path normalizer that absorbs paste-style escapes.

Regression coverage for the field bug where a user (via the platform UI
on iOS / voice / paste) entered a repo path with shell-style space
escapes:

    /Users/me/AnohterAgent\\ Projects/repo

Python's subprocess.Popen(cwd=...) does NOT interpret the backslash —
it treats it as a literal character — so the directory doesn't exist
on disk and the spawn fails with [Errno 2]. The normalizer rewrites
the most common shell-escape forms before the path reaches Popen so
the bridge stays forgiving of paste errors.

Lock the contract: each handled form gets a test, plus negative cases
that confirm we DON'T over-rewrite (e.g. legitimate single backslashes
in non-escape positions stay intact).
"""

from __future__ import annotations

import os

from routers.chat import normalize_repo_path


class TestNormalizeRepoPathNullAndEmpty:
    """Edge cases that should fall through to the caller's getcwd() default."""

    def test_returns_none_for_none(self):
        assert normalize_repo_path(None) is None

    def test_returns_none_for_empty_string(self):
        assert normalize_repo_path("") is None

    def test_returns_none_for_whitespace_only(self):
        # User pasted whitespace by accident — treat as "unset" so the
        # caller's os.getcwd() fallback wins instead of Popen rejecting
        # an empty path.
        assert normalize_repo_path("   ") is None
        assert normalize_repo_path("\t\n  ") is None


class TestNormalizeRepoPathSpaceEscape:
    """The actual bug from the field — backslash-escaped spaces."""

    def test_strips_single_escaped_space(self):
        # The exact path from the original error — modulo /repo suffix.
        assert (
            normalize_repo_path("/Users/me/AnohterAgent\\ Projects/repo")
            == "/Users/me/AnohterAgent Projects/repo"
        )

    def test_strips_multiple_escaped_spaces(self):
        # Some folder names have multiple spaces (e.g. backup paths).
        assert (
            normalize_repo_path("/tmp/My\\ Big\\ Folder/repo")
            == "/tmp/My Big Folder/repo"
        )

    def test_leaves_unescaped_spaces_alone(self):
        # A path entered correctly with plain spaces stays unchanged.
        # Without this, a no-op normalize would still pass — but a future
        # refactor that swaps str.replace for a regex could accidentally
        # touch normal spaces; this nails it down.
        assert (
            normalize_repo_path("/Users/me/AnohterAgent Projects/repo")
            == "/Users/me/AnohterAgent Projects/repo"
        )


class TestNormalizeRepoPathParenEscape:
    """Same paste-error class for parentheses (e.g. `Movies\\(2024\\)`)."""

    def test_strips_escaped_parens(self):
        assert (
            normalize_repo_path("/tmp/Movies\\(2024\\)/repo")
            == "/tmp/Movies(2024)/repo"
        )

    def test_leaves_unescaped_parens_alone(self):
        assert (
            normalize_repo_path("/tmp/Movies(2024)/repo") == "/tmp/Movies(2024)/repo"
        )


class TestNormalizeRepoPathTilde:
    """Home-directory shortcut — matches the shell + Python convention."""

    def test_expands_bare_tilde(self):
        # Bare ~ resolves to the user's home dir, no trailing slash.
        assert normalize_repo_path("~") == os.path.expanduser("~")

    def test_expands_tilde_prefix(self):
        # Most common form — relative to home.
        assert (
            normalize_repo_path("~/projects/repo")
            == os.path.expanduser("~/projects/repo")
        )

    def test_combines_tilde_with_escaped_space(self):
        # The compound paste error: ~/AnohterAgent\ Projects/repo. This
        # is the realistic mobile-paste shape — the user typed ~/ to
        # save typing the home prefix and copy-pasted the rest from a
        # terminal command. Both transforms must apply in one pass.
        expanded_home = os.path.expanduser("~")
        assert (
            normalize_repo_path("~/AnohterAgent\\ Projects/repo")
            == f"{expanded_home}/AnohterAgent Projects/repo"
        )

    def test_leaves_non_leading_tilde_alone(self):
        # ~ in the middle of a path is NOT a shell home shortcut —
        # don't rewrite it. (os.path.expanduser already enforces this,
        # but lock in the behavior so a future refactor can't drift.)
        assert (
            normalize_repo_path("/tmp/foo~bar/repo") == "/tmp/foo~bar/repo"
        )


class TestNormalizeRepoPathTrim:
    """Strip leading/trailing whitespace — common paste artifact."""

    def test_strips_leading_whitespace(self):
        assert normalize_repo_path("  /tmp/repo") == "/tmp/repo"

    def test_strips_trailing_whitespace(self):
        assert normalize_repo_path("/tmp/repo  ") == "/tmp/repo"

    def test_strips_trailing_newline(self):
        # Pasting from a multi-line clipboard often appends \n.
        assert normalize_repo_path("/tmp/repo\n") == "/tmp/repo"


class TestNormalizeRepoPathDoesNotOverRewrite:
    """Negative cases — confirm we don't touch legitimate backslashes."""

    def test_leaves_lone_backslash_alone(self):
        # A backslash NOT followed by ` `, `(`, or `)` is left intact.
        # This protects exotic-but-valid filenames on macOS/Linux that
        # might contain a literal backslash. (Rare, but the cost of
        # leaving them is zero.)
        assert (
            normalize_repo_path("/tmp/weird\\name/repo") == "/tmp/weird\\name/repo"
        )

    def test_leaves_double_backslash_in_non_escape_position_alone(self):
        # Double backslash mid-path — neither shell nor our normalizer
        # should touch it. Same protection as above.
        assert (
            normalize_repo_path("/tmp/weird\\\\name/repo")
            == "/tmp/weird\\\\name/repo"
        )
