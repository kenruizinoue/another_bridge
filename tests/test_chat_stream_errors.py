"""Error / cancel surfacing on the /chat/stream bridge.

The SSE wire format is `event: text|done|error`. The platform's
bridge dispatcher decides what to render in the chat bubble based on
which terminal event arrived — `done` means "save what we got", `error`
means "mark the message truncated/failed". Three distinct failure modes
must each surface as `event: error` (not silent done, not crashed
generator):

  - Claude exits non-zero (model error, auth, etc.)
  - Spawn fails (claude binary missing on the host)
  - Cancel landed mid-run (SIGTERM/SIGKILL hit Claude before it finished)

Plus tolerance for non-JSON garbage on stdout — Claude Code occasionally
prints warnings before the JSON stream starts, and they must not crash
the generator (otherwise a single noisy stderr line takes the whole
turn down).

The job's terminal status (failed/done) is also asserted because the
polling-reconnect path reads job.status to decide when to stop polling
— a stream that emitted SSE error but left the job stuck in "running"
would hang the client.
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


@pytest.fixture(autouse=True)
def _clean_session_map():
    chat_router._session_map.clear()  # type: ignore[attr-defined]
    yield
    chat_router._session_map.clear()  # type: ignore[attr-defined]


def _consume(resp: Any) -> str:
    return b"".join(resp.iter_bytes()).decode()


def _events(body: str) -> list[tuple[str, dict]]:
    """Parse the SSE body into a list of (event_type, data_dict) pairs.

    Skips the kickoff event (always first, asserted elsewhere) so tests
    can focus on the terminal event(s) that follow."""
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_line = next((ln for ln in block.splitlines() if ln.startswith("event:")), None)
        data_line = next((ln for ln in block.splitlines() if ln.startswith("data:")), None)
        if event_line is None or data_line is None:
            continue
        event_type = event_line[len("event:"):].strip()
        data = json.loads(data_line[len("data:"):].strip())
        out.append((event_type, data))
    return out


def _kickoff_job_id(body: str) -> str:
    first_block = body.split("\n\n")[0]
    data_line = next(line for line in first_block.splitlines() if line.startswith("data:"))
    return json.loads(data_line[len("data:"):].strip())["jobId"]


# ──────────────────────────────────────────────────────────────────────
# Claude exits non-zero → SSE error + job marked failed
# ──────────────────────────────────────────────────────────────────────


class _ExitNonZeroProc:
    def __init__(self, stderr_text: str = "model overloaded") -> None:
        self.stdout = iter([
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-x"}) + "\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = stderr_text
        self.pid = 1111
        self.returncode = 2

    def wait(self) -> int:
        return 2

    def poll(self) -> int:
        return 2


class TestClaudeExitsNonZero:
    def test_non_zero_exit_emits_sse_error_with_stderr(
        self, client: TestClient
    ) -> None:
        """Claude's stderr text gets surfaced verbatim in the SSE error
        event — the platform pipes this to the trace drawer so the user
        can see what Claude actually said."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _ExitNonZeroProc(stderr_text="auth failed")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        # First event after kickoff should be `error` (no done event before it)
        assert any(et == "error" and "auth failed" in d.get("error", "") for et, d in events), (
            f"expected error event with stderr text, got: {events}"
        )
        # Critically: NO done event — the platform uses `done` to commit
        # the assistant message, so an error must terminate the stream
        # instead of being followed by a done.
        assert not any(et == "done" for et, _ in events)

    def test_non_zero_exit_marks_job_failed(self, client: TestClient) -> None:
        """The polling-reconnect client uses job.status to decide when
        to stop polling. A non-zero exit must mark the job failed (not
        leave it running) so polling clients see done=True and switch
        to /messages?since=."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _ExitNonZeroProc()
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        job_id = _kickoff_job_id(body)
        snapshot = job_manager.get_chat_status(job_id)
        assert snapshot is not None
        assert snapshot["status"] == "failed"
        assert snapshot["done"] is True

    def test_non_zero_exit_with_empty_stderr_uses_exit_code_message(
        self, client: TestClient
    ) -> None:
        """When stderr is empty (Claude died without saying why), the
        error message should fall back to the exit code so the client
        gets *something* useful instead of an empty string."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _ExitNonZeroProc(stderr_text="")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        error_events = [d for et, d in events if et == "error"]
        assert error_events
        assert "exit" in error_events[0]["error"].lower() or "2" in error_events[0]["error"]


# ──────────────────────────────────────────────────────────────────────
# Spawn failure (binary missing) → SSE error + job marked failed
# ──────────────────────────────────────────────────────────────────────


class TestSpawnFailure:
    def test_file_not_found_emits_sse_error_and_marks_failed(
        self, client: TestClient
    ) -> None:
        """If the `claude` binary is missing on the host (FileNotFoundError
        from Popen), the user gets a directed message rather than a raw
        500. The streaming_subprocess context manager re-raises this so
        chat_stream can render a domain-specific SSE error."""
        with patch.object(
            claude_runner.subprocess, "Popen",
            side_effect=FileNotFoundError("claude: command not found"),
        ):
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        error_events = [d for et, d in events if et == "error"]
        assert error_events, f"expected error event, got: {events}"
        assert "spawn" in error_events[0]["error"].lower() or "claude" in error_events[0]["error"].lower()
        # No done event — spawn failure is terminal.
        assert not any(et == "done" for et, _ in events)

        # Job must transition out of running so polling clients stop.
        job_id = _kickoff_job_id(body)
        snapshot = job_manager.get_chat_status(job_id)
        assert snapshot is not None
        assert snapshot["status"] == "failed"


# ──────────────────────────────────────────────────────────────────────
# Mid-stream cancel → SSE error "cancelled by client" + job failed
# ──────────────────────────────────────────────────────────────────────


class _CancelMidStreamProc:
    """Simulates Claude getting SIGTERM'd: we deliver one chunk of text,
    then the proc 'exits' with returncode 0 (Claude often exits cleanly
    on SIGTERM) but the job's cancelled flag is set externally so
    chat_stream takes the cancel branch instead of the success branch."""

    def __init__(self) -> None:
        self.stdout = iter([
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-cancel"}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}) + "\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 2222
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


class TestMidStreamCancel:
    def test_cancel_emits_cancelled_sse_error(self, client: TestClient) -> None:
        """When the client cancels mid-stream, the proc may still exit
        cleanly (returncode 0) but the cancel flag on JobManager is set
        — chat_stream's check on is_cancelled must take precedence over
        the clean-exit branch and emit `cancelled by client`."""
        # Pre-create the job and pre-set cancelled so by the time the
        # stream's post-wait check runs, the flag is already True. This
        # is the "cancel landed during the run" simulation — JobManager
        # would normally have been flipped by /jobs/<id>/cancel landing
        # while the SSE response was in flight.
        original_create = job_manager.create

        created: list[str] = []

        def _capture_and_flag(kind: str):
            job = original_create(kind)
            created.append(job.job_id)
            # Flip cancel BEFORE the streamer reads stdout — this is
            # equivalent to the cancel HTTP call landing during the run.
            with job_manager._lock:  # type: ignore[attr-defined]
                job_manager._jobs[job.job_id].cancelled = True  # type: ignore[attr-defined]
            return job

        with patch.object(job_manager, "create", side_effect=_capture_and_flag):
            with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
                mock_popen.return_value = _CancelMidStreamProc()
                with client.stream(
                    "POST",
                    "/chat/stream",
                    json={"conversation_id": "conv-cancel", "message": "hi"},
                ) as resp:
                    body = _consume(resp)

        events = _events(body)
        error_events = [d for et, d in events if et == "error"]
        assert error_events, f"expected error event for cancel, got: {events}"
        assert "cancel" in error_events[0]["error"].lower()
        # No done event after a cancel — the partial text is kept in
        # accumulated_text but the bridge contract is "error wins".
        assert not any(et == "done" for et, _ in events)

        # Cancelled run must NOT persist session_id even though one was
        # captured — same rule as a non-zero exit. A retry should start
        # fresh.
        assert "conv-cancel" not in chat_router._session_map

        job_id = created[0]
        snapshot = job_manager.get_chat_status(job_id)
        assert snapshot is not None
        assert snapshot["status"] == "failed"


# ──────────────────────────────────────────────────────────────────────
# Non-JSON stdout line: tolerated, doesn't crash the stream
# ──────────────────────────────────────────────────────────────────────


class _NoisyStdoutProc:
    """Claude Code occasionally prints non-JSON warnings before / between
    JSON events (e.g. "Warning: anthropic SDK version mismatch"). The
    stream parser must skip them, not crash."""

    def __init__(self) -> None:
        self.stdout = iter([
            "Warning: this is not JSON at all\n",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-noisy"}) + "\n",
            "[deprecation] something will change\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n",
            "trailing garbage\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 3333
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


class TestNonJsonTolerance:
    def test_garbage_lines_dont_break_the_stream(self, client: TestClient) -> None:
        """A single noisy line must not take down the entire turn. The
        valid JSON events around the garbage should still produce a text
        chunk + clean done event."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _NoisyStdoutProc()
            with client.stream(
                "POST",
                "/chat/stream",
                json={"conversation_id": "conv-noisy", "message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        text_events = [d for et, d in events if et == "text"]
        done_events = [d for et, d in events if et == "done"]
        error_events = [d for et, d in events if et == "error"]

        # Exactly one text chunk made it through (the JSON one between
        # the two garbage lines), the run completed cleanly.
        assert len(text_events) == 1
        assert text_events[0]["chunk"] == "hello"
        assert len(done_events) == 1
        assert error_events == []

        # And the captured session_id from the valid system init was still
        # persisted — garbage didn't poison the success path.
        assert chat_router._session_map.get("conv-noisy") == "sess-noisy"


# ──────────────────────────────────────────────────────────────────────
# Done event payload shape
# ──────────────────────────────────────────────────────────────────────


class _NoSessionInitProc:
    """Claude exits cleanly but never emitted a system init — pathological
    but possible if Claude Code's output format changes. Done event must
    still fire (so the platform commits the message) just without the
    sessionId field."""

    def __init__(self) -> None:
        self.stdout = iter([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "no init"}]}}) + "\n",
        ])
        self.stderr = MagicMock()
        self.stderr.read.return_value = ""
        self.pid = 4444
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


class TestDoneEventPayload:
    def test_done_carries_session_id_when_captured(self, client: TestClient) -> None:
        """Standard happy path: system init seen → sessionId in done."""
        from tests.test_chat_session_lifecycle import _SuccessProc

        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _SuccessProc(session_id="sess-done-1")
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        done = [d for et, d in events if et == "done"]
        assert done == [{"sessionId": "sess-done-1"}]

    def test_done_is_empty_object_when_no_session_captured(
        self, client: TestClient
    ) -> None:
        """Defensive: clean exit but no init event → done is `{}` (NOT
        `{"sessionId": null}` or omitted entirely). The platform tolerates
        an empty object; an absent done would hang the client."""
        with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = _NoSessionInitProc()
            with client.stream(
                "POST",
                "/chat/stream",
                json={"message": "hi"},
            ) as resp:
                body = _consume(resp)

        events = _events(body)
        done = [d for et, d in events if et == "done"]
        assert done == [{}]
