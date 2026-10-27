"""A phone message never starts a second copy of a working session.

On October 27, 2026 a phone turn resumed a session while its terminal tab
was still working on the same reel script with background reviewers. The
phone copy redid the work and restarted the terminal's agents. The bridge
now reads Claude Code's per-process files: a session working in a terminal
tab (busy, or its lines or agents changed recently) holds phone messages
in the queue, and at most ANOTHER_BRIDGE_MAX_RUNNING_TURNS phone turns run
at once. "Send anyway" (force, or /resume/queue/release) skips both.

Every test uses fake session files in tmp folders, never the Mac's real
``~/.claude/sessions`` (conftest points the checker at an empty folder).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services import terminal_activity
from services.terminal_activity import TerminalWatch

from tests.test_sessions import _reset_resume_state, client, index  # noqa: F401  (fixtures)

# Records point at this test process, so the pid is alive. own_pid=1 makes
# the checker treat it as another process, not the bridge's own child.
OTHER_PID = os.getpid()


def _iso(seconds_ago: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _record(folder: Path, session_id: str, status: str = "idle", pid: int = OTHER_PID, name: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name or pid}.json"
    path.write_text(json.dumps({"pid": pid, "sessionId": session_id, "entrypoint": "cli", "status": status}))
    return path


def _append(path: Path, entrypoint: str, seconds_ago: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "entrypoint": entrypoint, "timestamp": _iso(seconds_ago)}) + "\n")


# ── The checker ───────────────────────────────────────────────────────


def test_no_other_process_means_no_state(tmp_path: Path) -> None:
    watch = TerminalWatch(tmp_path / "sessions", own_pid=1)
    assert watch.state("s1", None) is None
    assert TerminalWatch(tmp_path / "missing", own_pid=1).records() == {}


def test_a_dead_pid_is_ignored(tmp_path: Path) -> None:
    proc = subprocess.Popen(["true"])
    proc.wait()
    _record(tmp_path, "s1", status="busy", pid=proc.pid)
    assert TerminalWatch(tmp_path, own_pid=1).state("s1", None) is None


def test_garbage_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"pid": "x", "sessionId": 3}))
    assert TerminalWatch(tmp_path, own_pid=1).records() == {}


def test_busy_terminal_is_active(tmp_path: Path) -> None:
    _record(tmp_path, "s1", status="busy")
    state = TerminalWatch(tmp_path, own_pid=1).state("s1", None)
    assert state is not None and state.busy and state.active
    assert state.as_dict() == {"open": True, "busy": True, "active": True, "agents": 0}


def test_idle_and_quiet_terminal_is_open_but_not_active(tmp_path: Path) -> None:
    _record(tmp_path / "sessions", "s1")
    transcript = tmp_path / "proj" / "s1.jsonl"
    _append(transcript, "cli", seconds_ago=3600)
    state = TerminalWatch(tmp_path / "sessions", own_pid=1).state("s1", transcript)
    assert state is not None and not state.active and not state.busy


def test_idle_terminal_with_a_recent_line_is_active(tmp_path: Path) -> None:
    _record(tmp_path / "sessions", "s1")
    transcript = tmp_path / "proj" / "s1.jsonl"
    _append(transcript, "cli", seconds_ago=5)
    state = TerminalWatch(tmp_path / "sessions", own_pid=1).state("s1", transcript)
    assert state is not None and state.active


def test_the_bridges_own_lines_do_not_count(tmp_path: Path) -> None:
    # A phone turn just wrote (sdk-cli); the terminal's last line is old.
    _record(tmp_path / "sessions", "s1")
    transcript = tmp_path / "proj" / "s1.jsonl"
    _append(transcript, "cli", seconds_ago=3600)
    _append(transcript, "sdk-cli", seconds_ago=1)
    state = TerminalWatch(tmp_path / "sessions", own_pid=1).state("s1", transcript)
    assert state is not None and not state.active


def test_idle_terminal_with_running_agents_is_active(tmp_path: Path) -> None:
    # The GR00T case: the terminal's turn ended while its reviewers ran.
    _record(tmp_path / "sessions", "s1")
    transcript = tmp_path / "proj" / "s1.jsonl"
    _append(transcript, "cli", seconds_ago=3600)
    _append(transcript.parent / "s1" / "subagents" / "agent-a.jsonl", "cli", seconds_ago=10)
    _append(transcript.parent / "s1" / "subagents" / "agent-b.jsonl", "cli", seconds_ago=20)
    _append(transcript.parent / "s1" / "subagents" / "agent-c.jsonl", "sdk-cli", seconds_ago=5)
    state = TerminalWatch(tmp_path / "sessions", own_pid=1).state("s1", transcript)
    assert state is not None and state.active and state.agents == 2


def test_the_bridges_own_claude_child_is_not_a_terminal(tmp_path: Path) -> None:
    child = subprocess.Popen(["sleep", "30"])
    try:
        _record(tmp_path, "s1", status="busy", pid=child.pid)
        assert TerminalWatch(tmp_path).state("s1", None) is None  # own pid = this process
    finally:
        child.kill()
        child.wait()


def test_a_busy_holder_wins_over_an_idle_one(tmp_path: Path) -> None:
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _record(tmp_path, "s1", status="idle", name="a")
        _record(tmp_path, "s1", status="busy", pid=sleeper.pid, name="b")
        # own_pid=1: the sleeper is this process's child, not the bridge's.
        assert TerminalWatch(tmp_path, own_pid=1).records()["s1"]["status"] == "busy"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_errors_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch = TerminalWatch(tmp_path, own_pid=1)
    monkeypatch.setattr(watch, "records", lambda: (_ for _ in ()).throw(RuntimeError("format changed")))
    assert watch.state("s1", None) is None


# ── The routes ────────────────────────────────────────────────────────


@pytest.fixture
def terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake terminal tab holding sess-1. Returns a setter for its status."""
    folder = tmp_path / "claude-sessions"
    monkeypatch.setattr(terminal_activity, "watch", TerminalWatch(folder, own_pid=1))
    monkeypatch.setattr(sessions_router, "HOLD_POLL_SECONDS", 0.05)

    def set_status(status: str) -> None:
        _record(folder, "sess-1", status=status)

    set_status("busy")
    return set_status


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Prompts claude was started with (no real claude ever runs)."""
    prompts: list[str] = []

    @contextlib.contextmanager
    def fake_stream(args, cwd, job_id):
        prompts.append(args[args.index("-p") + 1])

        class P:
            stdout = ['{"type": "result"}\n']

        yield P()

    monkeypatch.setattr(sessions_router.claude_runner, "streaming_subprocess", fake_stream)
    return prompts


def _wait(predicate, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _stream(client: TestClient, **body) -> str:  # noqa: F811
    with client.stream("POST", "/sessions/sess-1/resume/stream", json=body) as r:
        assert r.status_code == 200
        return "".join(r.iter_text())


def test_a_busy_terminal_queues_the_phone_message(client: TestClient, terminal, ran) -> None:  # noqa: F811
    body = _stream(client, message="still waiting?")
    assert "event: queued" in body and '"reason": "terminal"' in body
    assert "event: done" in body
    assert ran == []  # no second copy started

    status = client.get("/sessions/sess-1/resume/status").json()
    assert status["queue_count"] == 1
    assert status["terminal"] == {"open": True, "busy": True, "active": True, "agents": 0}
    assert _wait(lambda: client.get("/sessions/sess-1/resume/status").json()["held"] == "terminal")

    terminal("idle")  # the terminal finishes; the queue runs the message
    assert _wait(lambda: ran == ["still waiting?"])
    assert _wait(lambda: client.get("/sessions/sess-1/resume/status").json()["queue_count"] == 0)


def test_a_quiet_terminal_tab_does_not_hold(client: TestClient, terminal, ran) -> None:  # noqa: F811
    terminal("idle")
    body = _stream(client, message="hi")
    assert "event: queued" not in body and "event: done" in body
    assert ran == ["hi"]


def test_send_anyway_runs_now(client: TestClient, terminal, ran) -> None:  # noqa: F811
    body = _stream(client, message="go", force=True)
    assert "event: queued" not in body
    assert ran == ["go"]


def test_release_lets_a_held_message_run(client: TestClient, terminal, ran) -> None:  # noqa: F811
    _stream(client, message="held one")
    time.sleep(0.2)
    assert ran == []
    released = client.post("/sessions/sess-1/resume/queue/release").json()
    assert released == {"ok": True, "session_id": "sess-1", "released": 1}
    assert _wait(lambda: ran == ["held one"])


def test_the_queue_endpoint_also_waits_for_the_terminal(client: TestClient, terminal, ran) -> None:  # noqa: F811
    assert client.post("/sessions/sess-1/resume/queue", json={"message": "later"}).json()["queued"] == 1
    time.sleep(0.2)
    assert ran == []
    terminal("idle")
    assert _wait(lambda: ran == ["later"])


def test_blocking_resume_refuses_a_busy_terminal(client: TestClient, terminal) -> None:  # noqa: F811
    r = client.post("/sessions/sess-1/resume", json={"message": "hi"})
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"]


def test_voice_turns_are_queued_too(client: TestClient, terminal) -> None:  # noqa: F811
    body = _stream(client, message="speak", voice=True)
    assert "event: queued" in body
    with sessions_router._queue_guard:
        assert sessions_router._queues["sess-1"][0]["voice"] is True


def test_the_running_turn_cap_queues_extra_turns(
    client: TestClient, ran, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(sessions_router, "ANOTHER_BRIDGE_MAX_RUNNING_TURNS", 1)
    monkeypatch.setattr(sessions_router, "HOLD_POLL_SECONDS", 0.05)
    assert sessions_router._try_start_resume("another-session") is True
    try:
        body = _stream(client, message="third turn")
        assert '"reason": "limit"' in body
        assert _wait(lambda: client.get("/sessions/sess-1/resume/status").json()["held"] == "limit")
        assert ran == []
    finally:
        sessions_router._end_resume("another-session")
    assert _wait(lambda: ran == ["third turn"])


def test_cards_show_the_terminal(client: TestClient, terminal) -> None:  # noqa: F811
    card = client.get("/sessions").json()["sessions"][0]
    assert card["terminal"] == {"open": True, "busy": True, "active": True, "agents": 0}


def test_cards_without_a_terminal_say_none(client: TestClient) -> None:  # noqa: F811
    assert client.get("/sessions").json()["sessions"][0]["terminal"] is None
