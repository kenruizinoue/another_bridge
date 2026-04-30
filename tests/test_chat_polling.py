"""Tests for the polling primitives that power the platform's bridge
reconnect path:

- Job.accumulated_text + JobManager.append_text + get_chat_status (unit)
- POST /chat/stream emits a kickoff event with statusUrl + poll seconds
- Streaming text chunks are mirrored into accumulated_text
- GET /jobs/<id>/chat/status returns the live snapshot
- Snapshot stays consistent across multiple poll requests during a run

These lock the contract the platform's bridgeRegistry + bridge-status
controller depend on. If a future refactor breaks accumulator consistency
or drops the kickoff fields, the platform's mobile-reconnect UX silently
regresses to "wait for graph completion + /messages?since= catch-up" —
exactly the v1 gap we just closed.
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
from routers import jobs as jobs_router
from services import claude_runner


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(jobs_router.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_jobs():
    """Reset jobs created during each test so module-level state doesn't
    leak. Mirrors the pattern in test_jobs_router_cancel_http.py."""
    before: set[str] = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    yield
    after = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    for job_id in after - before:
        job_manager._jobs.pop(job_id, None)  # type: ignore[attr-defined]


# Session-map cleanup moved to tests/conftest.py — the conversation_id
# → session_id store is now SQLite-backed and shared across the suite,
# so wiping it once per test there avoids redefining the autouse fixture
# in every chat-related test file.


# ──────────────────────────────────────────────────────────────────────
# JobManager.append_text + get_chat_status — pure unit
# ──────────────────────────────────────────────────────────────────────


class TestAccumulatorPrimitive:
    def test_append_text_concatenates_chunks(self) -> None:
        job = job_manager.create("chat_stream")
        job_manager.append_text(job.job_id, "hello ")
        job_manager.append_text(job.job_id, "world")
        snapshot = job_manager.get_chat_status(job.job_id)
        assert snapshot is not None
        assert snapshot["accumulatedText"] == "hello world"

    def test_append_text_skips_empty_chunks(self) -> None:
        # Empty chunks would cause pointless lock churn — skipped at the
        # API surface so the buffer never grows by zero. Caller doesn't
        # have to filter.
        job = job_manager.create("chat_stream")
        job_manager.append_text(job.job_id, "")
        job_manager.append_text(job.job_id, "x")
        snapshot = job_manager.get_chat_status(job.job_id)
        assert snapshot is not None
        assert snapshot["accumulatedText"] == "x"

    def test_append_text_unknown_job_is_noop(self) -> None:
        # Race protection: a poll arriving just after a job was reaped
        # could conceivably fire append_text. Must not crash.
        job_manager.append_text("nonexistent-job", "anything")
        # No exception = pass; assert nothing was magically created.
        assert job_manager.get_chat_status("nonexistent-job") is None

    def test_get_chat_status_unknown_returns_none(self) -> None:
        assert job_manager.get_chat_status("nonexistent-job") is None

    def test_get_chat_status_done_flag_flips_when_status_changes(self) -> None:
        # `done` is the polling client's terminator (status != "running").
        # Test the contract: running → False, done/failed → True.
        job = job_manager.create("chat_stream")
        snapshot = job_manager.get_chat_status(job.job_id)
        assert snapshot is not None
        assert snapshot["done"] is False
        assert snapshot["status"] == "running"

        job_manager.mark_done(job.job_id, {"any": "result"})
        snapshot = job_manager.get_chat_status(job.job_id)
        assert snapshot is not None
        assert snapshot["done"] is True
        assert snapshot["status"] == "done"


# ──────────────────────────────────────────────────────────────────────
# /jobs/<id>/chat/status HTTP endpoint
# ──────────────────────────────────────────────────────────────────────


class TestChatStatusEndpoint:
    def test_returns_404_shape_for_unknown_job(self, client: TestClient) -> None:
        # The platform proxy turns this into a 404 to the frontend; the
        # router itself returns 200 + a structured body so the proxy
        # doesn't have to inspect status codes.
        resp = client.get("/jobs/unknown-job/chat/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["done"] is True

    def test_returns_live_snapshot_for_running_job(self, client: TestClient) -> None:
        job = job_manager.create("chat_stream")
        job_manager.append_text(job.job_id, "partial response so far")

        resp = client.get(f"/jobs/{job.job_id}/chat/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["jobId"] == job.job_id
        assert body["status"] == "running"
        assert body["accumulatedText"] == "partial response so far"
        assert body["done"] is False
        # `elapsedSeconds` deliberately NOT surfaced on the chat-status
        # endpoint — the platform proxy strips it and the polling client
        # uses wall-clock locally. Lock the contract here so a future
        # revert that re-adds it bloats the proxy hop without value.
        assert "elapsedSeconds" not in body


# ──────────────────────────────────────────────────────────────────────
# /chat/stream wire-up: kickoff event + accumulator mirroring
# ──────────────────────────────────────────────────────────────────────


class _StubProc:
    """Same shape as the wiring-test stub but with multiple text events
    so we can verify the accumulator absorbs every chunk in order."""

    def __init__(self) -> None:
        self.stdout = iter([
            '{"type":"system","subtype":"init","session_id":"sess-1"}\n',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hello "}]}}\n',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"world"}]}}\n',
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
    with patch.object(claude_runner.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = _StubProc()
        yield mock_popen


def _consume(resp: Any) -> str:
    return b"".join(resp.iter_bytes()).decode()


class TestKickoffEventExtended:
    def test_kickoff_includes_status_url_and_poll_seconds(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """The kickoff event must carry the polling primitives so the
        platform's bridgeRegistry can register them — without these,
        the platform's bridge-status proxy has no statusUrl to hit and
        the frontend mobile-reconnect UX silently regresses."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-poll-1", "message": "hi"},
        ) as resp:
            body = _consume(resp)

        first_block = body.split("\n\n")[0]
        data_line = next(
            line for line in first_block.splitlines() if line.startswith("data:")
        )
        payload = json.loads(data_line[len("data:"):].strip())

        assert payload["jobId"]
        assert payload["cancelUrl"] == f"/jobs/{payload['jobId']}/cancel"
        assert payload["statusUrl"] == f"/jobs/{payload['jobId']}/chat/status"
        # Defaults from chat_router DEFAULT_POLL_EVERY/MAX_SECONDS — locked
        # in by this test so a future tweak that changes them gets caught.
        assert payload["pollEverySeconds"] == 5
        assert payload["pollMaxSeconds"] == 900

    def test_streamed_text_is_mirrored_into_accumulator(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """Every text chunk yielded as SSE must also land in
        job.accumulated_text — this is the contract the polling-reconnect
        path depends on. Without it, a polling client would see empty
        text even though the streaming client received chunks."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-poll-2", "message": "hi"},
        ) as resp:
            body = _consume(resp)

        # Pull the jobId from kickoff so we can look up the job.
        first_block = body.split("\n\n")[0]
        data_line = next(
            line for line in first_block.splitlines() if line.startswith("data:")
        )
        job_id = json.loads(data_line[len("data:"):].strip())["jobId"]

        snapshot = job_manager.get_chat_status(job_id)
        assert snapshot is not None
        # Both text chunks from _StubProc concatenated in order.
        assert snapshot["accumulatedText"] == "hello world"
        # Stream finished cleanly so the polling client should stop next tick.
        assert snapshot["done"] is True

    def test_chat_status_endpoint_consistent_during_streaming(
        self, client: TestClient, stub_popen: MagicMock
    ) -> None:
        """Once the stream completes, /jobs/<id>/chat/status returns the
        same accumulated text consistently across multiple polls — locks
        the contract that the polling client can re-request safely."""
        with client.stream(
            "POST",
            "/chat/stream",
            json={"conversation_id": "conv-poll-3", "message": "hi"},
        ) as resp:
            body = _consume(resp)

        first_block = body.split("\n\n")[0]
        data_line = next(
            line for line in first_block.splitlines() if line.startswith("data:")
        )
        job_id = json.loads(data_line[len("data:"):].strip())["jobId"]

        first_poll = client.get(f"/jobs/{job_id}/chat/status").json()
        second_poll = client.get(f"/jobs/{job_id}/chat/status").json()
        assert first_poll == second_poll
        assert first_poll["accumulatedText"] == "hello world"
