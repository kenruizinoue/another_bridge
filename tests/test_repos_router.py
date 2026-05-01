"""Coverage for the workspace-discovery surface:

  * ``routers.repos.list_workspace_repos`` — filter logic that walks
    one level under WORKSPACE_ROOT and returns dicts for repos with
    an origin remote. The agent's "what can I work on?" query reads
    from this; a bad filter returns the wrong universe of repos.

  * ``POST /tools/list_repos`` — the webhook tool the Engineering
    Manager Agent calls to enumerate repos. Three operator-visible
    branches: WORKSPACE_ROOT unset, set-but-missing, happy path.

The sub-helpers ``_git_origin_url`` and ``_parse_owner_repo`` are
already covered in tests/test_git_service.py; here we patch them so
this file stays focused on the router-level logic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from routers import repos as repos_router
from routers.repos import list_workspace_repos


# ──────────────────────────────────────────────────────────────────────
# list_workspace_repos: filter logic
# ──────────────────────────────────────────────────────────────────────


class TestListWorkspaceRepos:
    def test_returns_empty_when_workspace_root_missing(self) -> None:
        # WORKSPACE_ROOT not on disk → empty list, never raises.
        # Common operator misconfig (typo in .env path).
        assert list_workspace_repos("/no/such/dir") == []

    def test_returns_empty_when_workspace_root_blank(self) -> None:
        # WORKSPACE_ROOT="" is the unset state pydantic-settings
        # produces when the env var is absent. Must not crash.
        assert list_workspace_repos("") == []

    def test_skips_non_directory_entries(self, tmp_path: Path) -> None:
        # A loose file at the workspace root (e.g. a .DS_Store, a
        # backup tarball) must not turn into a phantom repo entry.
        (tmp_path / "stray.txt").write_text("not a repo")
        assert list_workspace_repos(str(tmp_path)) == []

    def test_skips_directories_without_dot_git(self, tmp_path: Path) -> None:
        # A plain directory with no .git/ — common for build outputs,
        # node_modules, downloaded archives. Filtered out.
        (tmp_path / "build_output").mkdir()
        assert list_workspace_repos(str(tmp_path)) == []

    def test_skips_repos_without_origin(self, tmp_path: Path) -> None:
        # A repo with a .git/ but no origin remote. Most often a
        # local scratch/backup. Filtered out so the agent doesn't
        # try to push branches to a non-existent remote.
        repo = tmp_path / "scratch"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch(
            "routers.repos._git_origin_url", return_value=None
        ):
            assert list_workspace_repos(str(tmp_path)) == []

    def test_returns_repos_with_origin_and_owner_repo(
        self, tmp_path: Path
    ) -> None:
        # Happy path: a real-looking repo with .git/ + an origin URL
        # pointing at GitHub. The returned dict must carry the four
        # fields the platform UI consumes verbatim.
        repo = tmp_path / "widgets"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch(
            "routers.repos._git_origin_url",
            return_value="git@github.com:acme/widgets.git",
        ), patch(
            "routers.repos._parse_owner_repo", return_value="acme/widgets"
        ):
            out = list_workspace_repos(str(tmp_path))
        assert len(out) == 1
        assert out[0] == {
            "name": "widgets",
            "path": str(repo),
            "origin_url": "git@github.com:acme/widgets.git",
            "owner_repo": "acme/widgets",
        }


# ──────────────────────────────────────────────────────────────────────
# POST /tools/list_repos: HTTP-level branches
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """Minimal app with only the repos router. Avoids the lifespan
    claude probe + reaper that wrapping ``main.app`` would trigger,
    and skips the auth gate so this file stays focused on the
    handler's own branches (auth coverage lives in test_auth.py)."""
    app = FastAPI()
    app.include_router(repos_router.router)
    return TestClient(app)


class TestListReposEndpoint:
    def test_workspace_root_unset_returns_actionable_error(
        self, client: TestClient
    ) -> None:
        # WORKSPACE_ROOT="" on the bridge → 200 with an error string
        # the agent surfaces back to the user. We use 200 (not 4xx)
        # because the LLM-tool-call return-shape contract is "always
        # 200 with a body the model reads".
        with patch.object(repos_router, "WORKSPACE_ROOT", ""):
            resp = client.post("/tools/list_repos", json={"args": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body
        assert "WORKSPACE_ROOT" in body["error"]

    def test_workspace_root_set_but_missing_returns_error(
        self, client: TestClient
    ) -> None:
        # Operator set WORKSPACE_ROOT to a path that doesn't exist
        # (typo, deleted directory, mounted-volume offline). Returns
        # an error string mentioning the bad path so they can fix it.
        with patch.object(repos_router, "WORKSPACE_ROOT", "/no/such/dir"):
            resp = client.post("/tools/list_repos", json={"args": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body
        assert "/no/such/dir" in body["error"]

    def test_happy_path_returns_repos_and_context_block(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        # Real workspace dir with one repo. The response shape is
        # part of the platform's __context__ contract — count + repos
        # at the top, plus a __context__.available_repos mirror that
        # the platform persists for next-turn context.
        repo = tmp_path / "widgets"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch.object(repos_router, "WORKSPACE_ROOT", str(tmp_path)), patch(
            "routers.repos._git_origin_url",
            return_value="git@github.com:acme/widgets.git",
        ), patch(
            "routers.repos._parse_owner_repo", return_value="acme/widgets"
        ):
            resp = client.post("/tools/list_repos", json={"args": {}})

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["repos"][0]["name"] == "widgets"
        assert body["repos"][0]["owner_repo"] == "acme/widgets"
        assert body["__context__"]["available_repos"] == body["repos"]
