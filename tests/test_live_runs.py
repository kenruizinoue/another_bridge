"""Re-attaching to a running turn (ticket 20).

The phone drops its stream whenever it leaves the chat or iOS suspends
it. The turn keeps running on the Mac, so every resume path publishes
its events to ``services.live_runs`` and ``GET /resume/attach`` lets the
phone watch the rest of the turn live instead of polling the transcript.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services import live_runs
from services.live_runs import LiveRun

from tests.test_sessions import _reset_resume_state, client, index  # noqa: F401  (fixtures)


def _delta(text: str) -> str:
    return json.dumps(
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}
    )


def _tool_message(description: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"description": description}}]},
        }
    )


# ── LiveRun ──────────────────────────────────────────────────────────


def test_follow_starts_at_the_message_being_written() -> None:
    run = LiveRun()
    run.publish("text", {"chunk": "old"})
    run.end_message()
    run.publish("text", {"chunk": "new"})
    run.finish()
    assert list(run.follow()) == [("text", {"chunk": "new"})]


def test_follow_from_zero_replays_the_whole_turn() -> None:
    run = LiveRun()
    run.publish("text", {"chunk": "a"})
    run.end_message()
    run.publish("text", {"chunk": "b"})
    run.finish()
    assert [d["chunk"] for _, d in run.follow(start=0)] == ["a", "b"]


def test_follow_yields_a_keepalive_while_quiet_then_live_events() -> None:
    run = LiveRun()
    got: list = []

    def consume() -> None:
        got.extend(run.follow(wait=0.05))

    t = threading.Thread(target=consume)
    t.start()
    time.sleep(0.2)
    run.publish("text", {"chunk": "late"})
    run.finish()
    t.join(timeout=2)
    assert not t.is_alive()
    assert None in got  # keepalive while nothing happened
    assert ("text", {"chunk": "late"}) in got


def test_finish_drops_the_run_but_a_follower_still_reads_its_tail() -> None:
    run = live_runs.start("s-tail")
    follower = run.follow(start=0, wait=0.05)
    run.publish("text", {"chunk": "last"})
    live_runs.finish("s-tail")
    assert live_runs.get("s-tail") is None
    assert [e for e in follower if e is not None] == [("text", {"chunk": "last"})]


# ── Publishing claude's stream-json lines ────────────────────────────


def test_publish_line_turns_deltas_and_tools_into_events() -> None:
    run = LiveRun()
    for raw in [_delta("Hel"), _delta("lo"), _tool_message("list files"), "", "not json", "42", _delta("next")]:
        sessions_router._publish_line(run, raw + "\n")
    run.finish()
    events = list(run.follow(start=0))
    assert events[:2] == [("text", {"chunk": "Hel"}), ("text", {"chunk": "lo"})]
    assert events[2][0] == "tool" and events[2][1]["label"] == "Bash: list files"
    # The finished message is in the transcript, so a new attach starts after it.
    assert list(run.follow()) == [("text", {"chunk": "next"})]


def test_start_and_end_resume_open_and_close_the_live_run() -> None:
    assert sessions_router._try_start_resume("s-life") is True
    try:
        assert live_runs.get("s-life") is not None
    finally:
        sessions_router._end_resume("s-life")
    assert live_runs.get("s-life") is None


# ── GET /sessions/{id}/resume/attach ─────────────────────────────────


def test_attach_with_nothing_running_ends_with_done(client: TestClient) -> None:  # noqa: F811
    with client.stream("GET", "/sessions/sess-1/resume/attach") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = "".join(r.iter_text())
    assert "event: done" in body
    assert "event: text" not in body


def test_attach_replays_the_current_message_then_follows_live(client: TestClient) -> None:  # noqa: F811
    assert sessions_router._try_start_resume("sess-1") is True
    run = live_runs.get("sess-1")
    run.publish("text", {"chunk": "finished message"})
    run.end_message()
    run.publish("text", {"chunk": "now "})

    def later() -> None:
        time.sleep(0.2)
        run.publish("tool", {"name": "Bash", "label": "Bash: run tests", "stat": None})
        sessions_router._end_resume("sess-1")

    threading.Thread(target=later).start()
    with client.stream("GET", "/sessions/sess-1/resume/attach") as r:
        body = "".join(r.iter_text())

    assert '"chunk": "now "' in body
    assert "finished message" not in body  # already in the transcript
    assert "Bash: run tests" in body
    assert body.rstrip().endswith('"session_id": "sess-1"}')  # done is last


# ── Every turn publishes, even with nobody watching ──────────────────


def test_stream_endpoint_publishes_what_it_streams(
    client: TestClient, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    seen: list = []
    real_start = live_runs.start

    def spy_start(session_id: str) -> LiveRun:
        run = real_start(session_id)
        seen.append(run)
        return run

    monkeypatch.setattr(sessions_router.live_runs, "start", spy_start)

    class FakeProc:
        stdout = [f"{_delta('hi')}\n", f"{_tool_message('look around')}\n"]

    @contextlib.contextmanager
    def fake_stream(args, cwd, job_id):
        yield FakeProc()

    monkeypatch.setattr(sessions_router.claude_runner, "streaming_subprocess", fake_stream)
    with client.stream("POST", "/sessions/sess-1/resume/stream", json={"message": "hi"}) as r:
        body = "".join(r.iter_text())

    assert '"chunk": "hi"' in body and "Bash: look around" in body and "event: done" in body
    events = list(seen[0].follow(start=0))
    assert ("text", {"chunk": "hi"}) in events
    assert live_runs.get("sess-1") is None  # cleared when claude exited


def test_queued_turns_stream_their_events_and_have_no_short_deadline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    ran: list[str] = []
    published: list = []
    real_publish = sessions_router._publish_line

    def spy_publish(run, raw):
        published.append(raw.strip())
        real_publish(run, raw)

    monkeypatch.setattr(sessions_router, "_publish_line", spy_publish)

    @contextlib.contextmanager
    def fake_stream(args, cwd, job_id):
        assert "--include-partial-messages" in args and "stream-json" in args
        ran.append(args[args.index("-p") + 1])

        class P:
            stdout = [f"{_delta('queued reply')}\n"]

        yield P()

    def no_blocking(*_a, **_k):
        raise AssertionError("text turns in the queue must stream, not block")

    monkeypatch.setattr(sessions_router.claude_runner, "streaming_subprocess", fake_stream)
    monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", no_blocking)

    client.post("/sessions/sess-1/resume/queue", json={"message": "first"})
    client.post("/sessions/sess-1/resume/queue", json={"message": "second"})
    for _ in range(60):
        st = client.get("/sessions/sess-1/resume/status").json()
        if len(ran) == 2 and not st["running"] and st["queue_count"] == 0:
            break
        time.sleep(0.1)

    assert ran == ["first", "second"]
    assert published.count(_delta("queued reply")) == 2
