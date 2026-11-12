"""Stop what a session is doing, wherever it runs (ticket 30): a phone
turn is interrupted, a working Mac tab gets Esc in its iTerm2 tab, and
optionally every Bash command its Claude runs is ended."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services import mac_terminal, process_view
from services.mac_terminal import OpenError
from services.session_index import SessionIndex

WRAP = (
    "/bin/zsh -c source /Users/ken/.claude/shell-snapshots/s.sh 2>/dev/null || true && "
    "eval '{cmd}' < /dev/null && pwd -P >| /tmp/claude-ab-cwd"
)


def _rows(tab_pid: int = 7225, bridge: int = 900) -> dict:
    return process_view.parse_ps(
        "\n".join(
            [
                f"  {bridge}     1   {bridge}  01:00:00  0.1 uvicorn",
                f" {tab_pid}     1  {tab_pid}  01:00:00  1.0 claude --dangerously-skip-permissions",
                f"11000  {tab_pid} 11000     05:00  2.0 " + WRAP.format(cmd="npm run dev"),
                f"11001 11000 11000     05:00  9.0 node vite",
            ]
        )
    )


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    d = tmp_path / "projects" / "-Users-dev"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/Users/dev", "timestamp": "2026-06-30T10:00:00Z",
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sessions_router, "session_index", SessionIndex(projects_dir=tmp_path / "projects"))
    monkeypatch.setattr(sessions_router.os, "getpid", lambda: 900)
    app = FastAPI()
    app.include_router(sessions_router.router)
    yield TestClient(app)
    with sessions_router._running_guard:
        sessions_router._running_resumes.clear()


def _mac_tab(monkeypatch, status: str = "busy", sent: bool = True) -> list:
    records = [{"pid": 7225, "sessionId": "s1", "entrypoint": "cli", "status": status}]
    monkeypatch.setattr(sessions_router.process_view, "read_records", lambda d: records)
    monkeypatch.setattr(sessions_router.process_view, "read_ps", lambda: _rows())
    monkeypatch.setattr(sessions_router.mac_terminal, "tty_of", lambda pid: "/dev/ttys004")
    pressed: list = []

    def fake_press(tty):
        pressed.append(tty)
        return sent

    monkeypatch.setattr(sessions_router.mac_terminal, "press_escape", fake_press)
    return pressed


def _stop(client, **body):
    return client.post("/sessions/s1/stop", json={"confirm": "s1", **body})


class TestStopSession:
    def test_a_working_mac_tab_gets_esc(self, client, monkeypatch) -> None:
        pressed = _mac_tab(monkeypatch)
        r = _stop(client).json()
        assert r["turn"] == "mac_tab" and pressed == ["/dev/ttys004"]
        assert r["commands_stopped"] == 0

    def test_an_idle_tab_gets_nothing(self, client, monkeypatch) -> None:
        pressed = _mac_tab(monkeypatch, status="idle")
        assert _stop(client).json()["turn"] == "none" and pressed == []

    def test_a_tab_outside_iterm_is_unsupported(self, client, monkeypatch) -> None:
        _mac_tab(monkeypatch, sent=False)
        assert _stop(client).json()["turn"] == "unsupported"

    def test_commands_too(self, client, monkeypatch) -> None:
        _mac_tab(monkeypatch)
        ended: list = []
        monkeypatch.setattr(sessions_router.process_view, "end_group", lambda pgid, grace=3.0: ended.append(pgid))
        r = _stop(client, commands=True).json()
        assert r["commands_stopped"] == 1 and ended == [11000]

    def test_a_phone_turn_is_interrupted(self, client, monkeypatch) -> None:
        _mac_tab(monkeypatch)
        calls: list = []
        monkeypatch.setattr(
            sessions_router, "_stop_phone_turn", lambda sid: calls.append(sid) or {"stopped": True, "paused": True}
        )
        with sessions_router._running_guard:
            sessions_router._running_resumes["s1"] = time.time()
        r = _stop(client).json()
        assert r["turn"] == "phone" and r["paused"] is True and calls == ["s1"]

    def test_the_confirm_must_be_the_id(self, client, monkeypatch) -> None:
        pressed = _mac_tab(monkeypatch)
        assert client.post("/sessions/s1/stop", json={"confirm": "other"}).status_code == 422
        assert pressed == []

    def test_unknown_session_404(self, client) -> None:
        assert client.post("/sessions/nope/stop", json={"confirm": "nope"}).status_code == 404

    def test_an_osascript_failure_is_a_502(self, client, monkeypatch) -> None:
        _mac_tab(monkeypatch)

        def boom(tty):
            raise OpenError("osascript exited 1: not allowed")

        monkeypatch.setattr(sessions_router.mac_terminal, "press_escape", boom)
        assert _stop(client).status_code == 502


class TestEscape:
    def test_tty_of_reads_ps(self) -> None:
        def run(args, **kw):
            return subprocess.CompletedProcess(args, 0, "ttys004\n", "")

        assert mac_terminal.tty_of(123, run=run) == "/dev/ttys004"

    @pytest.mark.parametrize("out", ["??\n", "", "console\n"])
    def test_tty_of_without_a_terminal(self, out) -> None:
        assert mac_terminal.tty_of(1, run=lambda a, **k: subprocess.CompletedProcess(a, 0, out, "")) is None

    def test_the_script_finds_the_tab_by_tty_and_sends_only_esc(self) -> None:
        script = mac_terminal.iterm_escape_script("/dev/ttys004")
        assert 'if tty of aSession is "/dev/ttys004" then' in script
        assert "write text (character id 27) newline no" in script

    @pytest.mark.parametrize("bad", ["/dev/ttys004\" then do shell script \"x", "ttys004", "/dev/disk0"])
    def test_the_script_refuses_anything_but_a_tty(self, bad) -> None:
        with pytest.raises(OpenError):
            mac_terminal.iterm_escape_script(bad)

    def test_press_escape_reports_sent_or_missing(self) -> None:
        sent = lambda a, **k: subprocess.CompletedProcess(a, 0, "sent\n", "")  # noqa: E731
        missing = lambda a, **k: subprocess.CompletedProcess(a, 0, "missing\n", "")  # noqa: E731
        assert mac_terminal.press_escape("/dev/ttys004", run=sent) is True
        assert mac_terminal.press_escape("/dev/ttys004", run=missing) is False
