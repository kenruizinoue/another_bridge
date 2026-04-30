"""Session-id round-tripping for the /chat/stream bridge.

Locks the contract between Claude Code's session_id and the platform's
conversation_id:

  1. First message on a fresh conversation_id spawns a Claude Code
     session WITHOUT --resume and captures session_id from the system
     init event.
  2. Subsequent messages on the same conversation_id spawn WITH
     --resume <captured> so the Claude Code session continues with
     full transcript context (this is the whole point of the bridge —
     otherwise every turn is a stateless one-shot).
  3. A FAILED first run does NOT store the session_id, so the next
     attempt starts fresh instead of trying to --resume into a session
     Claude Code never had a chance to register.
  4. Calls without a conversation_id never touch the session map —
     one-shot curl tests / dashboard probes don't need session
     continuity.

Without these guarantees the bridge silently regresses to "every turn
spawns a fresh session", which is functionally fine but burns context
budget on every message. The kind of regression unit tests on
build_claude_args wouldn't catch — has to be the /chat/stream wire-up.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobs import job_manager
from routers import chat as chat_router
from services.session_store import session_store
from services import claude_runner


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_jobs():
    before: set[str] = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    yield
    after = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    for job_id in after - before:
        job_manager._jobs.pop(job_id, None)  # type: ignore[attr-defined]


# Session-store cleanup moved to tests/conftest.py — the
# conversation_id → session_id store is now SQLite-backed via
# services/session_store, and conftest's autouse fixture wipes it
# between every test.


class _SuccessProc:
    """stream-json output emulating a clean Claude Code run that emits
    a system init (with session_id) plus one assistant text chunk.
    Configurable session_id so tests can distinguish first vs second runs."""

    def __init__(self, session_id: str = "sess-fresh") -> None:
        self.stdout = iter([
            json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 1234
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


class _FailingProc:
    """Claude exits non-zero — the chat_stream code path that decides
    NOT to persist the captured session_id."""

    def __init__(self) -> None:
        self.stdout = iter([
            # Even a failing run emits a system init event before exiting,
            # so capturing/persistence has to be gated on returncode, not
            # on whether captured_session_id is set.
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-doomed"}) + "\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = "claude blew up"
        self.pid = 5678
        self.returncode = 1

    def wait(self) -> int:
        return 1

    def poll(self) -> int:
        return 1


def _consume(resp: Any) -> str:
    return b"".join(resp.iter_bytes()).decode()


def _last_popen_args(mock_popen: MagicMock) -> list[str]:
    """Pull the args= list from the most recent subprocess.Popen call.

    chat.py invokes claude_runner.streaming_subprocess(args=cmd, cwd=...)
    which in turn calls subprocess.Popen(args, ...). The args list is the
    first positional argument to Popen.
    """
    return list(mock_popen.call_args.args[0])


# ──────────────────────────────────────────────────────────────────────
# Session map: capture on first success, resume on second
# ──────────────────────────────────────────────────────────────────────


class TestSessionMapCapture:
    def test_first_message_does_not_pass_resume_flag(
        self, client: TestClient
    ) -> None:
        """Fresh conversation_id → session map empty → no --resume on the
        spawned claude command. The flag should appear ONLY when there's
        a previously captured session to continue."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-A")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-fresh", "message": "hello"},
            ) as resp:
                _consume(resp)

        cmd = _last_popen_args(mock_popen)
        assert "--resume" not in cmd, f"unexpected --resume in fresh-call cmd: {cmd}"

    def test_first_message_stores_captured_session_id(
        self, client: TestClient
    ) -> None:
        """After a clean run, the conversation_id → session_id mapping
        must be persisted. This is what the second call reads from."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-A")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-1", "message": "hi"},
            ) as resp:
                _consume(resp)

        assert session_store.get_session("conv-1") == "sess-A"

    def test_second_message_passes_resume_with_stored_session_id(
        self, client: TestClient
    ) -> None:
        """Same conversation_id reused → spawn must include
        --resume <stored>. This is the bridge's core continuity feature —
        without it, every turn is a one-shot."""
        # Pre-populate as if a prior run already captured the session.
        session_store.set_session("conv-2", "sess-A")

        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            # Returning a different session_id here doesn't matter — the
            # test asserts on the cmd args sent INTO Popen (resume flag),
            # not what Claude Code emits back.
            mock_popen.return_value = _SuccessProc(session_id="sess-B")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-2", "message": "follow-up"},
            ) as resp:
                _consume(resp)

        cmd = _last_popen_args(mock_popen)
        assert "--resume" in cmd, f"expected --resume in cmd, got: {cmd}"
        # --resume <id> are adjacent — verify the value too, not just the flag.
        resume_idx = cmd.index("--resume")
        assert cmd[resume_idx + 1] == "sess-A"

    def test_second_message_overwrites_session_id_with_new_capture(
        self, client: TestClient
    ) -> None:
        """When Claude Code rotates a session_id mid-conversation
        (it can — long sessions get re-issued), the next clean run must
        update the map. Otherwise the third turn would --resume into a
        stale session and Claude would 404 it."""
        session_store.set_session("conv-rotate", "sess-A")

        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-B")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-rotate", "message": "third turn"},
            ) as resp:
                _consume(resp)

        assert session_store.get_session("conv-rotate") == "sess-B"


# ──────────────────────────────────────────────────────────────────────
# Failure path: do NOT persist a captured session_id
# ──────────────────────────────────────────────────────────────────────


class TestFailedRunDoesNotPersistSession:
    def test_non_zero_exit_leaves_session_map_unchanged(
        self, client: TestClient
    ) -> None:
        """Even though Claude emitted a system init (so the runner
        captured a session_id), a non-zero exit means we treat the run
        as failed and SKIP persistence. The next attempt should start
        fresh (no --resume) instead of trying to continue a session
        Claude Code never finished bootstrapping."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _FailingProc()
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-failed", "message": "boom"},
            ) as resp:
                _consume(resp)

        assert session_store.get_session("conv-failed") is None

    def test_failed_run_leaves_prior_session_id_intact(
        self, client: TestClient
    ) -> None:
        """If the conversation already had a stored session_id from a
        previous successful turn, a subsequent FAILED turn must NOT wipe
        it — a retry should still --resume into the working session
        rather than starting from scratch."""
        session_store.set_session("conv-retry", "sess-good")

        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _FailingProc()
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-retry", "message": "boom"},
            ) as resp:
                _consume(resp)

        # The old session id survives the failed run, so the next attempt
        # can pick up where the working turn left off.
        assert session_store.get_session("conv-retry") == "sess-good"


# ──────────────────────────────────────────────────────────────────────
# No conversation_id → session map untouched
# ──────────────────────────────────────────────────────────────────────


class TestOneShotMode:
    def test_no_conversation_id_does_not_resume(self, client: TestClient) -> None:
        """A one-shot call (no conversation_id) must never pass --resume
        even if the session map is populated for some other conversation."""
        session_store.set_session("conv-other", "sess-A")

        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-B")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "one-shot probe"},
            ) as resp:
                _consume(resp)

        cmd = _last_popen_args(mock_popen)
        assert "--resume" not in cmd

    def test_no_conversation_id_does_not_write_session_map(
        self, client: TestClient
    ) -> None:
        """Successful one-shot runs capture a session_id internally (for
        the SSE done event) but must NOT pollute the session map —
        there's no conversation_id key to file it under."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-X")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "one-shot"},
            ) as resp:
                body = _consume(resp)

        # No conversation_id → no row written for any conversation_id.
        # Sanity check: the captured session_id from the SSE done event
        # below is ALSO not stored under any conversation key.
        assert session_store.get_session("sess-X") is None
        # Sanity: the done event still carries the captured session_id so
        # an SDK consumer that wants to thread it back later can.
        done_block = next(
            block for block in body.split("\n\n")
            if block.startswith("event: done")
        )
        data_line = next(line for line in done_block.splitlines() if line.startswith("data:"))
        payload = json.loads(data_line[len("data:"):].strip())
        assert payload == {"sessionId": "sess-X"}
