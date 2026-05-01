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

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobs import job_manager
from routers import chat as chat_router
from services import claude_runner


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router)
    return TestClient(app)


# Job + session-store cleanup moved to tests/conftest.py — see the
# comment in test_chat_polling.py for context.


@pytest.fixture(autouse=True)
def _bypass_repo_validation():
    """These tests cover the normalize→Popen wiring (the original bug
    was the literal ``\\ `` reaching Popen). After the security
    hardening, /chat/stream also runs the path through
    validate_repo_path — which would reject the synthetic
    ``/tmp/AnohterAgent\\ Projects/repo`` paths these tests use because
    they don't exist on disk. validate_repo_path is locked down by
    its own tests (test_repo_path_traversal.py + test_chat_security.py),
    so here we replace it with a passthrough that returns the input
    verbatim. Wiring tests stay about wiring; security tests stay
    about security."""
    with patch.object(
        chat_router,
        "validate_repo_path",
        side_effect=lambda p, *_a, **_k: (p, None),
    ):
        yield


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
    """Patch subprocess.Popen at the services.claude_runner seam — that's
    where the chat endpoint's spawn now goes through. Returns the
    MagicMock so each test can read .call_args off it (cwd kwarg
    still captured the same way)."""
    with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
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
        """When repo_path is missing AND CODING_REPO_PATH is unset, the
        endpoint falls back to ``os.getcwd()``. We blank the env var so
        the wiring contract under test is the cwd fallback specifically;
        the CODING_REPO_PATH-set case is covered separately by the
        security tests."""
        with patch.object(chat_router, "CODING_REPO_PATH", ""):
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
        fallback kicks in instead of Popen choking on an empty cwd or
        a stray tab. CODING_REPO_PATH blanked so we exercise the cwd
        leg of the fallback chain."""
        with patch.object(chat_router, "CODING_REPO_PATH", ""):
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


class TestKickoffEvent:
    """Cancel-propagation contract: /chat/stream MUST emit a kickoff SSE
    event as the very first message, carrying jobId + cancelUrl. The
    platform bridge dispatcher reads it to wire abort → cancel POST.
    Without kickoff, the platform can't address the JobManager job and
    the subprocess keeps running on the Mac after the user cancels —
    exactly the bug this contract was added to prevent. Mirrors the
    async-webhook cancel_url contract used by instruct_planning /
    instruct_implementation so the two paths behave identically."""

    def test_first_event_is_kickoff_with_jobid_and_cancel_url(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-k1", "message": "hi"},
        ) as resp:
            # Decode the body so we can split on SSE block boundaries.
            body = b"".join(resp.iter_bytes()).decode()

        blocks = [b for b in body.split("\n\n") if b.strip()]
        assert blocks, "expected at least one SSE event"

        # The very first non-empty block must be the kickoff event.
        first = blocks[0]
        assert "event: kickoff" in first.splitlines()[0]
        # Pull the data line and parse the JSON payload.
        data_line = next(line for line in first.splitlines() if line.startswith("data:"))
        payload = json.loads(data_line[len("data:") :].strip())
        assert "jobId" in payload, "kickoff payload missing jobId"
        assert isinstance(payload["jobId"], str) and payload["jobId"]
        assert payload.get("cancelUrl") == f"/jobs/{payload['jobId']}/cancel"

    def test_kickoff_jobid_matches_an_attached_jobmanager_job(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """The jobId in the kickoff event must be the same job_id that
        JobManager has the subprocess registered against — otherwise
        the platform's POST /jobs/<id>/cancel would 404 instead of
        actually killing the subprocess. This locks the contract so a
        future refactor that mints a separate "cancel id" for the
        kickoff event (defensible but wrong) trips this test."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-k2", "message": "hi"},
        ) as resp:
            body = b"".join(resp.iter_bytes()).decode()

        first = body.split("\n\n")[0]
        data_line = next(line for line in first.splitlines() if line.startswith("data:"))
        kickoff_job_id = json.loads(data_line[len("data:") :].strip())["jobId"]

        # JobManager singleton should hold a job by this id (created by
        # chat_stream and registered before the first SSE event).
        from jobs import job_manager

        assert job_manager.get(kickoff_job_id) is not None, (
            "kickoff jobId is not addressable in JobManager — "
            "POST /jobs/<id>/cancel from the platform would 404"
        )
