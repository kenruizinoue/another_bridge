"""Voice-mode resume turns: ``voice: true`` switches the run to
``--output-format json --json-schema`` so the reply always carries a
``speech`` summary. Covers the SSE voice stream (text + speech + done,
schema flags on the args, error surfacing), the blocking endpoint's
speech field, the queue worker's voice flags, and the transcript
unwrap that renders the structured turn as its reply text."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services.claude_runner import ClaudeResult
from services.session_index import SessionIndex, _assistant_blocks

CWD = "/Users/dev/proj"


def _events() -> list[dict]:
    return [
        {
            "type": "user",
            "cwd": CWD,
            "timestamp": "2026-06-30T10:00:00Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-30T10:00:01Z",
            "uuid": "a1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        },
    ]


def _write_session(root: Path, encoded_cwd: str, session_id: str, events: list[dict]) -> Path:
    d = root / encoded_cwd
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def index(tmp_path: Path) -> SessionIndex:
    _write_session(tmp_path, "-Users-dev-proj", "sess-1", _events())
    return SessionIndex(projects_dir=tmp_path)


@pytest.fixture
def client(index: SessionIndex, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(sessions_router, "session_index", index)
    app = FastAPI()
    app.include_router(sessions_router.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_resume_state():
    yield
    with sessions_router._running_guard:
        sessions_router._running_resumes.clear()
    with sessions_router._queue_guard:
        sessions_router._queues.clear()
        sessions_router._workers.clear()


def _ok_result(stdout: str) -> ClaudeResult:
    return ClaudeResult(
        returncode=0, stdout=stdout, stderr="", timed_out=False,
        cancelled=False, duration_seconds=0.1, spawn_error=None,
    )


VOICE_STDOUT = json.dumps(
    {
        "result": "ignored raw result",
        "structured_output": {"reply": "Full reply text.", "speech": "Done, tests are green."},
    }
)


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    current_type = None
    for line in body.splitlines():
        if line.startswith("event: "):
            current_type = line[len("event: "):]
        elif line.startswith("data: ") and current_type:
            events.append((current_type, json.loads(line[len("data: "):])))
            current_type = None
    return events


def test_voice_stream_runs_schema_and_emits_text_speech_done(client, monkeypatch):
    captured: dict = {}

    def fake_run_blocking(args, cwd, timeout_seconds, job_id):
        captured["args"] = args
        captured["cwd"] = cwd
        return _ok_result(VOICE_STDOUT)

    monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", fake_run_blocking)

    resp = client.post(
        "/sessions/sess-1/resume/stream", json={"message": "run the tests", "voice": True}
    )
    assert resp.status_code == 200
    events = _sse_events(resp.text)

    assert ("text", {"chunk": "Full reply text."}) in events
    assert ("speech", {"speech": "Done, tests are green."}) in events
    assert events[-1] == ("done", {"session_id": "sess-1"})

    args = captured["args"]
    assert "--json-schema" in args
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert schema["required"] == ["reply", "speech"]
    fmt = args[args.index("--output-format") + 1]
    assert fmt == "json"
    assert captured["cwd"] == CWD


def test_voice_stream_without_structured_output_skips_speech(client, monkeypatch):
    stdout = json.dumps({"result": "plain reply, schema ignored"})
    monkeypatch.setattr(
        sessions_router.claude_runner, "run_blocking",
        lambda args, cwd, timeout_seconds, job_id: _ok_result(stdout),
    )
    resp = client.post("/sessions/sess-1/resume/stream", json={"message": "hi", "voice": True})
    events = _sse_events(resp.text)
    assert ("text", {"chunk": "plain reply, schema ignored"}) in events
    assert not any(t == "speech" for t, _ in events)
    assert events[-1][0] == "done"


def test_voice_stream_surfaces_claude_failure_as_error_event(client, monkeypatch):
    failed = ClaudeResult(
        returncode=2, stdout="", stderr="boom", timed_out=False,
        cancelled=False, duration_seconds=0.1, spawn_error=None,
    )
    monkeypatch.setattr(
        sessions_router.claude_runner, "run_blocking",
        lambda args, cwd, timeout_seconds, job_id: failed,
    )
    resp = client.post("/sessions/sess-1/resume/stream", json={"message": "hi", "voice": True})
    events = _sse_events(resp.text)
    assert events[-1][0] == "error"
    assert "exited 2" in events[-1][1]["message"]


def test_voice_stream_clears_inflight_marker(client, monkeypatch):
    monkeypatch.setattr(
        sessions_router.claude_runner, "run_blocking",
        lambda args, cwd, timeout_seconds, job_id: _ok_result(VOICE_STDOUT),
    )
    client.post("/sessions/sess-1/resume/stream", json={"message": "hi", "voice": True})
    status = client.get("/sessions/sess-1/resume/status").json()
    assert status["running"] is False


def test_blocking_resume_voice_returns_speech(client, monkeypatch):
    monkeypatch.setattr(
        sessions_router.claude_runner, "run_blocking",
        lambda args, cwd, timeout_seconds, job_id: _ok_result(VOICE_STDOUT),
    )
    resp = client.post("/sessions/sess-1/resume", json={"message": "hi", "voice": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Full reply text."
    assert body["speech"] == "Done, tests are green."


def test_queue_voice_turn_runs_with_schema_flags(client, monkeypatch):
    captured: dict = {}
    ran = threading.Event()

    def fake_run_blocking(args, cwd, timeout_seconds, job_id):
        captured["args"] = args
        ran.set()
        return _ok_result(VOICE_STDOUT)

    monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", fake_run_blocking)

    resp = client.post("/sessions/sess-1/resume/queue", json={"message": "hi", "voice": True})
    assert resp.status_code == 200
    assert ran.wait(timeout=5), "queue worker never ran the voice turn"
    assert "--json-schema" in captured["args"]


def test_voice_result_falls_back_to_plain_text_stdout():
    reply, speech = sessions_router._voice_result("not json at all")
    assert reply == ""
    assert speech is None


def test_transcript_unwraps_voice_turn_to_reply(tmp_path):
    voice_turn_text = json.dumps({"reply": "The visible answer.", "speech": "Short summary."})
    text, tools = _assistant_blocks([{"type": "text", "text": voice_turn_text}])
    assert text == "The visible answer."
    assert tools == []


def test_transcript_leaves_normal_json_looking_text_alone():
    text, _ = _assistant_blocks([{"type": "text", "text": '{"foo": 1}'}])
    assert text == '{"foo": 1}'


def test_transcript_surfaces_structured_output_tool_use_as_reply_text():
    # The REAL shape the CLI writes under --json-schema (verified live
    # 2026-07-24): the final assistant turn is a StructuredOutput tool_use
    # whose input carries the {reply, speech} object.
    content = [
        {"type": "thinking", "thinking": "internal"},
        {
            "type": "tool_use",
            "name": "StructuredOutput",
            "input": {"reply": "2+2 equals 4.", "speech": "Two plus two equals four."},
        },
    ]
    text, tools = _assistant_blocks(content)
    assert text == "2+2 equals 4."
    assert tools == []


def test_other_tool_use_blocks_still_count_as_tools():
    content = [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]
    text, tools = _assistant_blocks(content)
    assert text is None
    assert len(tools) == 1


def test_every_mobile_resume_disallows_mac_voice_tools(client, monkeypatch):
    captured: dict = {}

    def fake_run_blocking(args, cwd, timeout_seconds, job_id):
        captured.setdefault("calls", []).append(args)
        return _ok_result(VOICE_STDOUT)

    monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", fake_run_blocking)

    client.post("/sessions/sess-1/resume", json={"message": "text turn"})
    client.post("/sessions/sess-1/resume/stream", json={"message": "voice turn", "voice": True})
    for args in captured["calls"]:
        i = args.index("--disallowed-tools")
        assert "voicemode" in args[i + 1]
