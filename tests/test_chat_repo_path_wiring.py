"""Wiring test: confirm POST /chat/stream actually routes the user's
``repo_path`` through ``normalize_repo_path`` before handing it to
subprocess.Popen as ``cwd``.

Companion to test_chat_normalize_repo_path.py — that file is the unit
contract for the normalizer. This file is the integration contract that
proves the endpoint *invokes* it. Without this, someone could rip out the
``normalize_repo_path()`` call in routers/chat.py and every unit test
would still pass while the production bug returned.

The original bug: a user pasted a shell-escaped path
``/Users/me/AnohterAgent\\ Projects/repo`` into the platform's repo_path
input → another_coder Popen'd that string verbatim → spawn failed with
``[Errno 2] No such file or directory``. This test fakes Popen and asserts
``cwd=`` arrived in the rewritten form.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobs import job_manager
from routers import chat as chat_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_jobs():
    """Reset jobs the test created so the module-level singleton doesn't
    leak between tests. Same pattern as test_jobs_router_cancel_http.py."""
    before: set[str] = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    yield
    after = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    for job_id in after - before:
        job_manager._jobs.pop(job_id, None)  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _clean_session_map():
    """Same hygiene for the conversation_id → session_id map. Without
    this, a successful test that captures a session_id would let the
    next test's --resume see a stale id and skip the system-init path."""
    chat_router._session_map.clear()  # type: ignore[attr-defined]
    yield
    chat_router._session_map.clear()  # type: ignore[attr-defined]


class _StubProc:
    """Minimal stand-in for subprocess.Popen. The chat_stream generator
    iterates ``proc.stdout`` and then calls ``proc.wait()``. We feed it
    one harmless system-init line so the loop runs once and exits."""

    def __init__(self) -> None:
        self.stdout = iter([
            # Valid stream-json system init event so _extract_session_id
            # captures something (lets the success path complete cleanly).
            '{"type":"system","subtype":"init","session_id":"sess-123"}\n',
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 99999
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


@pytest.fixture
def stub_popen():
    """Patch subprocess.Popen at the routers.chat seam (the import the
    endpoint actually uses) so we can capture the kwargs without
    spawning a real claude subprocess. Returns the MagicMock so each
    test can read .call_args off it."""
    with patch.object(chat_router.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = _StubProc()
        yield mock_popen


def _consume_stream(resp: Any) -> None:
    """Drain the SSE response so the generator runs to completion. The
    Popen call happens lazily inside the generator, not at request time,
    so we have to actually iterate to trigger it."""
    for _ in resp.iter_bytes():
        pass


# ──────────────────────────────────────────────────────────────────────
# The actual wiring contract
# ──────────────────────────────────────────────────────────────────────


class TestRepoPathNormalization:
    def test_escaped_space_in_repo_path_is_normalized_before_popen(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """The original bug: ``\\ `` makes it through to Popen and fails.
        After the fix, the cwd kwarg should arrive with a plain space."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "conversation_id": "conv-1",
                "message": "hi",
                # The exact paste-error shape from the field report.
                "repo_path": "/tmp/AnohterAgent\\ Projects/repo",
            },
        ) as resp:
            _consume_stream(resp)

        assert stub_popen.call_count == 1
        cwd = stub_popen.call_args.kwargs["cwd"]
        assert cwd == "/tmp/AnohterAgent Projects/repo"
        # Hard guard: the literal backslash must not survive. Without
        # this, a future regression where the normalizer copies the raw
        # string would still pass the equality assertion above if the
        # path happened to be split-and-joined cleanly.
        assert "\\" not in cwd

    def test_tilde_prefix_is_expanded_before_popen(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """Mobile/voice users often type ``~/foo`` to save keystrokes."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "conversation_id": "conv-2",
                "message": "hi",
                "repo_path": "~/projects/repo",
            },
        ) as resp:
            _consume_stream(resp)

        cwd = stub_popen.call_args.kwargs["cwd"]
        assert cwd == os.path.expanduser("~/projects/repo")
        # No raw tilde reaches Popen.
        assert "~" not in cwd

    def test_omitted_repo_path_falls_back_to_cwd(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """When repo_path is missing entirely, the endpoint falls back to
        ``os.getcwd()`` — the normalizer must NOT swallow that path with
        a None return that then gets cast to "None" or empty string."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-3", "message": "hi"},
        ) as resp:
            _consume_stream(resp)

        cwd = stub_popen.call_args.kwargs["cwd"]
        # os.getcwd() returns an absolute path; that's the contract
        # we're locking in, not the specific value.
        assert os.path.isabs(cwd)
        assert cwd == os.getcwd()

    def test_whitespace_only_repo_path_falls_back_to_cwd(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """Pasted-whitespace edge — the normalizer returns None so the
        ``or os.getcwd()`` fallback kicks in instead of Popen choking on
        an empty cwd or a stray tab."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "conversation_id": "conv-4",
                "message": "hi",
                "repo_path": "   \t  ",
            },
        ) as resp:
            _consume_stream(resp)

        cwd = stub_popen.call_args.kwargs["cwd"]
        assert cwd == os.getcwd()
