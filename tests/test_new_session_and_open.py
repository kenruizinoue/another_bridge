"""Start a session from the phone and open a session on the Mac
(tickets 24 and 25).

Like test_sessions.py, a minimal app with only the sessions router over a
tmp_path transcript tree; claude and osascript are faked.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services import mac_terminal
from services.claude_runner import ClaudeResult
from services.session_index import SessionIndex
from tests import fake_turns


def _write_session(root: Path, session_id: str, cwd: str) -> None:
    d = root / "projects" / "-encoded"
    d.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "user", "cwd": cwd, "timestamp": "2026-06-30T10:00:00Z", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-06-30T10:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    (d / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every (command, app) "Open on Mac" sent to osascript."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(sessions_router.mac_terminal, "open_tab", lambda command, app: calls.append((command, app)))
    return calls


@pytest.fixture
def client(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, opened) -> TestClient:
    _write_session(tmp_path, "sess-1", str(home))
    monkeypatch.setattr(sessions_router, "session_index", SessionIndex(projects_dir=tmp_path / "projects"))
    monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_NEW_SESSION_CWD", home)
    monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_TERMINAL_APP", "auto")
    monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_OPEN_SKIP_PERMISSIONS", True)
    monkeypatch.setattr(sessions_router.mac_terminal, "ITERM_APP_PATH", tmp_path / "iTerm.app")  # missing
    monkeypatch.setattr(sessions_router, "OPEN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(sessions_router, "_terminal", lambda session_id: None)
    monkeypatch.setattr(sessions_router.mac_terminal, "resume_processes", lambda session_id, exclude_parent: [])
    app = FastAPI()
    app.include_router(sessions_router.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    with sessions_router._running_guard:
        sessions_router._running_resumes.clear()
    with sessions_router._queue_guard:
        sessions_router._queues.clear()
        sessions_router._workers.clear()
    with sessions_router.live_runs._guard:
        sessions_router.live_runs._runs.clear()
    with sessions_router._open_guard:
        sessions_router._pending_opens.clear()


def _events(body: str) -> list[tuple[str, dict]]:
    out = []
    for frame in body.split("\n\n"):
        lines = frame.strip().splitlines()
        if len(lines) == 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def _stream_lines() -> list[str]:
    return [
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                     "delta": {"type": "text_delta", "text": "Hi"}}}) + "\n",
        json.dumps({"type": "result", "result": "Hi"}) + "\n",
    ]


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """Every streamed claude spawn: args, cwd and the fake process (its
    stdin holds what the bridge wrote). A new session's fake claude writes
    its transcript, like the real one."""
    calls: list[dict] = []

    def _start(args, cwd):
        if "--session-id" in args:
            _write_session(tmp_path, _flag(args, "--session-id"), cwd)

    fake_turns.install(monkeypatch, sessions_router, _stream_lines(), calls=calls, on_spawn=_start)
    return calls


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


# ── New session from the phone ────────────────────────────────────────


class TestNewSession:
    def test_streams_the_new_id_first_then_the_turn(self, client, spawned, home) -> None:
        with client.stream("POST", "/sessions/new/stream", json={"message": "hello"}) as r:
            assert r.status_code == 200
            events = _events("".join(r.iter_text()))
        kinds = [k for k, _ in events]
        assert kinds[0] == "session" and kinds[-1] == "done" and "text" in kinds
        sid = events[0][1]["session_id"]
        assert str(uuid.UUID(sid)) == sid
        call = spawned[0]
        assert _flag(call["args"], "--session-id") == sid
        assert "--resume" not in call["args"]
        assert call["cwd"] == str(home)
        assert call["proc"].stdin.texts()[0] == "hello"
        assert _flag(call["args"], "--model") == sessions_router.ANOTHER_CODER_RESUME_MODEL
        assert events[-1][1]["session_id"] == sid

    def test_a_name_is_passed_to_claude(self, client, spawned) -> None:
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi", "name": " Nov/5  Errands "}) as r:
            _events("".join(r.iter_text()))
        assert _flag(spawned[0]["args"], "--name") == "Nov/5 Errands"

    def test_no_name_lets_claude_title_it(self, client, spawned) -> None:
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            _events("".join(r.iter_text()))
        assert "--name" not in spawned[0]["args"]

    def test_a_name_with_control_characters_is_refused(self, client, spawned) -> None:
        r = client.post("/sessions/new/stream", json={"message": "hi", "name": "a\nb"})
        assert r.status_code == 422
        assert spawned == []

    def test_an_empty_message_is_refused(self, client, spawned) -> None:
        assert client.post("/sessions/new/stream", json={"message": "  "}).status_code == 422
        assert spawned == []

    def test_images_go_on_stdin(self, client, spawned) -> None:
        img = {"media_type": "image/png", "data": "aGk="}
        with client.stream("POST", "/sessions/new/stream", json={"message": "look", "images": [img]}) as r:
            events = _events("".join(r.iter_text()))
        call = spawned[0]
        assert '"image"' in call["proc"].stdin.lines[0]
        assert _flag(call["args"], "--session-id") == events[0][1]["session_id"]

    def test_the_turn_cap_refuses_it_without_spawning(self, client, spawned, monkeypatch) -> None:
        monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_MAX_RUNNING_TURNS", 1)
        with sessions_router._running_guard:
            sessions_router._running_resumes["other"] = time.time()
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            events = _events("".join(r.iter_text()))
        assert events[0][0] == "error" and events[0][1]["reason"] == "limit"
        assert spawned == []

    def test_send_anyway_passes_the_cap(self, client, spawned, monkeypatch) -> None:
        monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_MAX_RUNNING_TURNS", 1)
        with sessions_router._running_guard:
            sessions_router._running_resumes["other"] = time.time()
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi", "force": True}) as r:
            _events("".join(r.iter_text()))
        assert len(spawned) == 1

    def test_the_session_event_waits_for_the_transcript(self, client, monkeypatch, tmp_path) -> None:
        # claude is slow to write the transcript: the id goes out only once
        # the session can be opened, so the chat never gets a 404.
        import threading

        def slow_start(args, cwd):
            sid = _flag(args, "--session-id")
            threading.Timer(0.3, lambda: _write_session(tmp_path, sid, cwd)).start()

        fake_turns.install(monkeypatch, sessions_router, _stream_lines(), on_spawn=slow_start)
        started = time.time()
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            events = _events("".join(r.iter_text()))
        assert time.time() - started >= 0.3
        sid = events[0][1]["session_id"]
        assert events[0][0] == "session"
        assert client.get(f"/sessions/{sid}").status_code == 200

    def test_the_session_event_goes_out_after_the_wait_anyway(self, client, monkeypatch) -> None:
        monkeypatch.setattr(sessions_router, "NEW_SESSION_READY_SECONDS", 0.05)

        fake_turns.install(monkeypatch, sessions_router, _stream_lines())  # never writes a transcript
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            kinds = [k for k, _ in _events("".join(r.iter_text()))]
        assert kinds[0] == "session" and kinds[-1] == "done"

    def test_the_turn_is_not_running_after_it_ends(self, client, spawned) -> None:
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            sid = _events("".join(r.iter_text()))[0][1]["session_id"]
        deadline = time.time() + 2
        while sessions_router._resume_started_at(sid) is not None and time.time() < deadline:
            time.sleep(0.01)
        assert sessions_router._resume_started_at(sid) is None


# ── Phone turns run long work in the foreground (ticket 24) ───────────


class TestPhoneTurnNote:
    def test_new_and_streamed_turns_carry_the_note(self, client, spawned) -> None:
        with client.stream("POST", "/sessions/new/stream", json={"message": "hi"}) as r:
            _events("".join(r.iter_text()))
        with client.stream("POST", "/sessions/sess-1/resume/stream", json={"message": "hi"}) as r:
            _events("".join(r.iter_text()))
        assert len(spawned) == 2
        for call in spawned:
            assert _flag(call["args"], "--append-system-prompt") == sessions_router.PHONE_TURN_NOTE

    def test_blocking_and_voice_turns_carry_the_note(self, client, monkeypatch) -> None:
        calls = []

        def fake_run_blocking(args, cwd, timeout_seconds, job_id):
            calls.append(args)
            return ClaudeResult(0, json.dumps({"result": "ok", "structured_output": {"reply": "ok", "speech": "ok"}}),
                                "", False, False, 0.1)

        monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", fake_run_blocking)
        client.post("/sessions/sess-1/resume", json={"message": "text"})
        client.post("/sessions/sess-1/resume", json={"message": "voice", "voice": True})
        assert len(calls) == 2
        for args in calls:
            assert _flag(args, "--append-system-prompt") == sessions_router.PHONE_TURN_NOTE

    def test_the_note_says_to_run_long_work_in_the_foreground(self) -> None:
        note = sessions_router.PHONE_TURN_NOTE
        assert "run_in_background" in note and "foreground" in note and "10 minutes" in note


# ── Open on Mac ───────────────────────────────────────────────────────


class TestOpenOnMac:
    def test_opens_a_quiet_session_now(self, client, opened, home) -> None:
        r = client.post("/sessions/sess-1/open-on-mac")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "status": "opened", "app": "terminal"}
        command, app = opened[0]
        assert command == f"cd {home} && claude --dangerously-skip-permissions --resume sess-1"
        assert app == "terminal"  # iTerm2 is missing in the fixture

    def test_uses_iterm_when_installed(self, client, opened, monkeypatch, tmp_path) -> None:
        (tmp_path / "iTerm.app").mkdir()
        assert client.post("/sessions/sess-1/open-on-mac").json()["app"] == "iterm"
        assert opened[0][1] == "iterm"

    def test_unknown_session_404(self, client, opened) -> None:
        assert client.post("/sessions/nope/open-on-mac").status_code == 404
        assert opened == []

    def test_a_missing_folder_is_refused(self, client, opened, tmp_path, monkeypatch) -> None:
        _write_session(tmp_path, "gone", str(tmp_path / "deleted"))
        assert client.post("/sessions/gone/open-on-mac").status_code == 422
        assert opened == []

    def test_a_session_already_open_in_a_terminal_is_not_opened_twice(self, client, opened, monkeypatch) -> None:
        monkeypatch.setattr(sessions_router, "_terminal", lambda session_id: object())
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "already_open"
        assert opened == []

    def test_a_tab_still_at_its_startup_question_counts_as_open(self, client, opened, monkeypatch) -> None:
        # Claude Code has no process record until "trust this folder" is
        # answered; its --resume process is enough (2026-11-05).
        monkeypatch.setattr(
            sessions_router.mac_terminal, "resume_processes", lambda session_id, exclude_parent: [7016]
        )
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "already_open"
        assert opened == []

    def test_waits_for_a_running_phone_turn_then_opens(self, client, opened) -> None:
        assert sessions_router._try_start_resume("sess-1")
        body = client.post("/sessions/sess-1/open-on-mac").json()
        assert body["status"] == "waiting"
        time.sleep(0.1)
        assert opened == []  # never while the phone turn writes the session
        assert client.get("/sessions/sess-1/resume/status").json()["opening_on_mac"] is True
        sessions_router._end_resume("sess-1")
        deadline = time.time() + 2
        while not opened and time.time() < deadline:
            time.sleep(0.01)
        assert len(opened) == 1
        assert client.get("/sessions/sess-1/resume/status").json()["opening_on_mac"] is False

    def test_waits_for_queued_messages_too(self, client, opened) -> None:
        with sessions_router._queue_guard:
            sessions_router._queues["sess-1"] = [{"preview": "next", "message": "next", "images": []}]
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "waiting"
        time.sleep(0.1)
        assert opened == []
        with sessions_router._queue_guard:
            sessions_router._queues.pop("sess-1")
        deadline = time.time() + 2
        while not opened and time.time() < deadline:
            time.sleep(0.01)
        assert len(opened) == 1

    def test_a_second_tap_while_waiting_opens_only_once(self, client, opened) -> None:
        assert sessions_router._try_start_resume("sess-1")
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "waiting"
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "waiting"
        sessions_router._end_resume("sess-1")
        time.sleep(0.3)
        assert len(opened) == 1

    def test_gives_up_after_the_wait_limit(self, client, opened, monkeypatch) -> None:
        monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_OPEN_WAIT_SECONDS", 0.05)
        assert sessions_router._try_start_resume("sess-1")
        assert client.post("/sessions/sess-1/open-on-mac").json()["status"] == "waiting"
        time.sleep(0.3)
        sessions_router._end_resume("sess-1")
        time.sleep(0.1)
        assert opened == []
        assert client.get("/sessions/sess-1/resume/status").json()["opening_on_mac"] is False

    def test_an_osascript_failure_is_a_502_with_its_reason(self, client, monkeypatch) -> None:
        def fail(command, app):
            raise mac_terminal.OpenError("osascript exited 1: Not authorized to send Apple events (-1743)")

        monkeypatch.setattr(sessions_router.mac_terminal, "open_tab", fail)
        r = client.post("/sessions/sess-1/open-on-mac")
        assert r.status_code == 502
        assert "-1743" in r.json()["detail"]
