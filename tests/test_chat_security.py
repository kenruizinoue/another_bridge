"""Security regression tests for /chat/stream's repo_path gate.

Three things stack to make this load-bearing:

  1. Every Claude Code spawn includes
     ``--dangerously-skip-permissions`` (claude_runner.build_claude_args).
  2. cwd is fully caller-controlled on /chat/stream.
  3. The bridge sits on a public ngrok URL guarded only by a single
     shared X-Coder-Key.

Without the validate_repo_path gate, a leaked X-Coder-Key would let
an attacker run arbitrary shell anywhere on the host's filesystem
just by setting ``repo_path`` to ``/Users/me`` or ``~/.ssh``. The
sibling endpoints /tools/instruct_planning and /tools/instruct_-
implementation already enforce this gate; these tests lock the
contract that /chat/stream does too.

If a future refactor strips the validation call from
``routers/chat.py``, every test in this file should fail.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import chat as chat_router
from services import claude_runner


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router)
    # raise_server_exceptions=False so HTTPException(400) surfaces as
    # a 400 response instead of bubbling out of the test client. Mirrors
    # how uvicorn would surface it to the platform's bridge dispatcher.
    return TestClient(app, raise_server_exceptions=False)


class _NoSpawnProc:
    """Sentinel — fails the test if instantiated. Validation must reject
    the request BEFORE Popen runs; if a test reaches subprocess.Popen
    despite passing a malicious path, that's the regression."""

    def __init__(self) -> None:  # pragma: no cover — only fires on bug
        raise AssertionError(
            "Popen called despite invalid repo_path — validation gate missed!"
        )


def test_outside_workspace_root_is_rejected_with_400(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """The exact attack scenario: caller sets repo_path to a directory
    outside the configured workspace root. Must be a 400 with no
    subprocess spawn — the platform's bridge dispatcher then surfaces
    the rejection cleanly to the LLM/trace."""
    workspace = tmp_path / "workspace"
    legit_repo = workspace / "legit_repo"
    legit_repo.mkdir(parents=True)
    (legit_repo / ".git").mkdir()

    outside = tmp_path / "outside"
    outside_repo = outside / "secret_repo"
    outside_repo.mkdir(parents=True)
    (outside_repo / ".git").mkdir()

    # Point WORKSPACE_ROOT (which validate_repo_path reads via default
    # arg) at the legit workspace. The attacker is reaching for a path
    # that exists but is outside.
    monkeypatch.setattr(
        "routers.repos.WORKSPACE_ROOT",
        str(workspace),
        raising=False,
    )

    with patch.object(claude_runner.subprocess, "Popen", side_effect=_NoSpawnProc):
        resp = client.post(
            "/chat/stream",
            json={
                "conversation_id": "conv-attack",
                "message": "read ~/.ssh/id_rsa",
                "repo_path": str(outside_repo),
            },
        )

    assert resp.status_code == 400
    body = resp.json()
    # The exact error string isn't load-bearing, but the response
    # MUST include the rejected path's keyword so the platform's
    # log-only consumer can grep for it.
    assert "workspace" in body["detail"].lower()


def test_nonexistent_path_is_rejected_with_400(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """A path that doesn't resolve to a directory (typo, deleted repo,
    typo'd home reference) gets the same 400. validate_repo_path
    rejects on isdir before any workspace-bounds check — locks the
    earliest-possible rejection."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "routers.repos.WORKSPACE_ROOT",
        str(workspace),
        raising=False,
    )

    with patch.object(claude_runner.subprocess, "Popen", side_effect=_NoSpawnProc):
        resp = client.post(
            "/chat/stream",
            json={
                "message": "hi",
                "repo_path": "/this/does/not/exist",
            },
        )

    assert resp.status_code == 400


def test_symlink_escape_is_rejected(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """The most subtle attack: a symlink inside the workspace pointing
    at a directory outside. validate_repo_path's ``os.path.realpath``
    resolves the symlink BEFORE the commonpath check, so escape via
    symlink fails the workspace bounds check."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "outside"
    outside_repo = outside / "real_repo"
    outside_repo.mkdir(parents=True)
    (outside_repo / ".git").mkdir()

    # Plant a symlink inside workspace that resolves outside.
    escape_link = workspace / "looks_legit"
    escape_link.symlink_to(outside_repo)

    monkeypatch.setattr(
        "routers.repos.WORKSPACE_ROOT",
        str(workspace),
        raising=False,
    )

    with patch.object(claude_runner.subprocess, "Popen", side_effect=_NoSpawnProc):
        resp = client.post(
            "/chat/stream",
            json={
                "message": "hi",
                "repo_path": str(escape_link),
            },
        )

    assert resp.status_code == 400


class _OkProc:
    """Successful Claude Code stream stub — system init then close."""

    def __init__(self) -> None:
        self.stdout = iter([
            '{"type":"system","subtype":"init","session_id":"sess-ok"}\n',
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 1
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


def test_path_inside_workspace_is_accepted(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """Positive case: a real git repo inside workspace_root passes
    validation and reaches Popen with the resolved absolute path as
    cwd. Belt-and-suspenders against an over-zealous future refactor
    that bans every repo_path."""
    workspace = tmp_path / "workspace"
    legit_repo = workspace / "legit_repo"
    legit_repo.mkdir(parents=True)
    (legit_repo / ".git").mkdir()

    monkeypatch.setattr(
        "routers.repos.WORKSPACE_ROOT",
        str(workspace),
        raising=False,
    )

    with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = _OkProc()
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "conversation_id": "conv-ok",
                "message": "hi",
                "repo_path": str(legit_repo),
            },
        ) as resp:
            for _ in resp.iter_bytes():
                pass

    assert mock_popen.call_count == 1
    cwd = mock_popen.call_args.kwargs["cwd"]
    # Resolved realpath, not the raw input — this is what the spawn
    # actually runs in.
    assert os.path.realpath(cwd) == os.path.realpath(str(legit_repo))


def test_legacy_no_workspace_root_falls_back_to_isdir_only(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """When WORKSPACE_ROOT is unset (legacy CODING_REPO_PATH-only
    deployments), validate_repo_path falls back to a bare isdir
    check — same as before the workspace gate landed. Locks the
    backward-compat behavior so existing deployments don't break.

    This case is the trade-off: legacy deployments accept any path
    that exists, which is weaker than the workspace-bounded form.
    The README + ARCHITECTURE.md call this out so users know to
    set WORKSPACE_ROOT for production deployments."""
    legit_repo = tmp_path / "legit_repo"
    legit_repo.mkdir()
    (legit_repo / ".git").mkdir()

    monkeypatch.setattr("routers.repos.WORKSPACE_ROOT", "", raising=False)

    with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = _OkProc()
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "conversation_id": "conv-legacy",
                "message": "hi",
                "repo_path": str(legit_repo),
            },
        ) as resp:
            for _ in resp.iter_bytes():
                pass

    assert mock_popen.call_count == 1
