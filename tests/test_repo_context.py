"""Coverage for ``services.repo_context.build_selected_repo_context``.

Shapes the ``__context__.selected_repo`` payload the bridge returns
from the planning + implementation routes. The platform persists
this on the assistant message and surfaces it in the next turn's
[Conversation context] block, so the Developer Agent can read
``repo_path`` + ``owner_repo`` structurally instead of parsing
markdown out of conversation history.

A bug here is invisible in local dev (the dict still serialises;
the agent just gets a context entry missing ``owner_repo``) but
breaks the platform's cross-turn continuity for tickets — well
worth locking.
"""

from __future__ import annotations

from unittest.mock import patch

from services.repo_context import build_selected_repo_context


class TestBuildSelectedRepoContext:
    def test_path_and_name_always_present_even_without_origin(self) -> None:
        # No origin remote configured (scratch dir, fresh init, or a
        # detached repo). The path + name fields must still come back
        # so the platform can render a minimal selected_repo card.
        with patch(
            "services.repo_context._git_origin_url", return_value=None
        ):
            ctx = build_selected_repo_context("/Users/me/Projects/scratch")
        assert ctx == {
            "path": "/Users/me/Projects/scratch",
            "name": "scratch",
        }
        assert "owner_repo" not in ctx

    def test_origin_without_parseable_owner_repo_drops_field(self) -> None:
        # Origin URL exists but ``_parse_owner_repo`` couldn't extract
        # an owner/repo (private gitlab, gitea, ssh-only with non-
        # standard format). Don't fabricate a value — drop the field
        # so the consumer treats the repo as "GitHub-unknown".
        with patch(
            "services.repo_context._git_origin_url",
            return_value="git@gitea.internal:team/project.git",
        ), patch(
            "services.repo_context._parse_owner_repo", return_value=None
        ):
            ctx = build_selected_repo_context("/Users/me/Projects/project")
        assert ctx["path"] == "/Users/me/Projects/project"
        assert ctx["name"] == "project"
        assert "owner_repo" not in ctx

    def test_github_origin_includes_owner_repo(self) -> None:
        # The common case: origin is github.com/<owner>/<repo>.git.
        # ``owner_repo`` lands in the payload so the platform can
        # build clickable PR / issue links from it.
        with patch(
            "services.repo_context._git_origin_url",
            return_value="git@github.com:acme/widgets.git",
        ), patch(
            "services.repo_context._parse_owner_repo",
            return_value="acme/widgets",
        ):
            ctx = build_selected_repo_context("/Users/me/Projects/widgets")
        assert ctx == {
            "path": "/Users/me/Projects/widgets",
            "name": "widgets",
            "owner_repo": "acme/widgets",
        }

    def test_trailing_slash_in_path_does_not_break_name(self) -> None:
        # Defensive — some callers pass paths with trailing slashes
        # (env vars copy-pasted from filesystem GUIs do this). The
        # name extraction must strip them; otherwise os.path.basename
        # returns "" and the platform UI shows an empty card title.
        with patch(
            "services.repo_context._git_origin_url", return_value=None
        ):
            ctx = build_selected_repo_context("/Users/me/Projects/widgets/")
        assert ctx["name"] == "widgets"
