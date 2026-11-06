"""Talking to a running phone turn and handling queued messages
(ticket 26): send into the turn, Stop, and Send now, Delete (with Undo)
and Edit for queued messages."""

from __future__ import annotations

import json
import time

import pytest

from routers import sessions as sessions_router
from services import turn_input
from services.turn_input import TurnInput
from tests import fake_turns
from tests.test_sessions import _reset_resume_state, client, index  # noqa: F401  (fixtures)


def _wait(cond, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _echo(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


RESULT = json.dumps({"type": "result", "subtype": "success", "result": "ok"})


@pytest.fixture(autouse=True)
def _clean_ticket_26_state():
    yield
    with sessions_router._queue_guard:
        sessions_router._paused.clear()
        sessions_router._trash.clear()
    with turn_input._guard:
        turn_input._inputs.clear()


# ── TurnInput ─────────────────────────────────────────────────────────


class TestTurnInput:
    def test_first_message_is_a_stream_json_user_line_with_images(self) -> None:
        stdin = fake_turns.FakeStdin()
        TurnInput(stdin).send_first("look", [{"media_type": "image/png", "data": "QUJD"}])
        line = json.loads(stdin.lines[0])
        assert line["type"] == "user"
        assert line["message"]["content"][0] == {"type": "text", "text": "look"}
        assert line["message"]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}

    def test_a_steer_is_written_and_tracked_until_claude_echoes_it(self) -> None:
        stdin = fake_turns.FakeStdin()
        inp = TurnInput(stdin)
        inp.send_first("first", [])
        entry = inp.steer("also skip android")
        assert entry is not None and entry["state"] == "sent"
        assert fake_turns.message_text(stdin.lines[1]) == "also skip android"
        assert inp.mark_read("first") is None  # the first message's echo
        read = inp.mark_read("also skip android")
        assert read is not None and read["id"] == entry["id"] and read["state"] == "read"
        assert inp.steers() == [{"id": entry["id"], "text": "also skip android", "state": "read"}]

    def test_nothing_is_written_after_close(self) -> None:
        stdin = fake_turns.FakeStdin()
        inp = TurnInput(stdin)
        inp.close()
        assert stdin.closed and not inp.open
        assert inp.steer("late") is None
        assert not inp.interrupt()

    def test_interrupt_is_a_control_request(self) -> None:
        stdin = fake_turns.FakeStdin()
        assert TurnInput(stdin).interrupt()
        line = json.loads(stdin.lines[0])
        assert line["type"] == "control_request" and line["request"] == {"subtype": "interrupt"}

    def test_a_broken_pipe_closes_the_input(self) -> None:
        class Broken:
            def write(self, _):
                raise BrokenPipeError

            def flush(self):
                pass

            def close(self):
                pass

        inp = TurnInput(Broken())
        assert inp.steer("x") is None
        assert not inp.open

    def test_replayed_text_ignores_tool_results(self) -> None:
        tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
        assert turn_input.replayed_text(tool) is None
        assert turn_input.replayed_text(json.loads(_echo("hi"))) == "hi"
        assert turn_input.replayed_text({"type": "assistant"}) is None


# ── Routes ────────────────────────────────────────────────────────────


@pytest.fixture
def procs(monkeypatch) -> list:
    out: list = []
    fake_turns.install_controlled(monkeypatch, sessions_router, out)
    return out


def _start_turn(client, procs, message: str = "build the reel"):  # noqa: F811
    """A queued text turn runs on the worker thread and stays running."""
    client.post("/sessions/sess-1/resume/queue", json={"message": message})
    assert _wait(lambda: procs and procs[-1].stdin.lines)
    return procs[-1]


def _status(client):  # noqa: F811
    return client.get("/sessions/sess-1/resume/status").json()


class TestInject:
    def test_a_message_goes_into_the_running_turn(self, client, procs) -> None:  # noqa: F811
        proc = _start_turn(client, procs)
        assert _status(client)["can_inject"] is True
        r = client.post("/sessions/sess-1/resume/inject", json={"message": "also skip android"})
        assert r.status_code == 200 and r.json()["status"] == "sent_mid_turn"
        assert proc.stdin.texts() == ["build the reel", "also skip android"]
        steer_id = r.json()["id"]
        assert _status(client)["steers"] == [{"id": steer_id, "text": "also skip android", "state": "sent"}]

        proc.emit(_echo("also skip android"))  # claude read it
        assert _wait(lambda: _status(client)["steers"][0]["state"] == "read")
        run = sessions_router.live_runs.get("sess-1")
        assert ("steer", {"id": steer_id, "state": "read"}) in run.events
        proc.emit(RESULT)
        proc.end()

    def test_the_result_closes_the_input_so_late_messages_queue(self, client, procs) -> None:  # noqa: F811
        proc = _start_turn(client, procs)
        proc.emit(RESULT)
        assert _wait(lambda: proc.stdin.closed)
        r = client.post("/sessions/sess-1/resume/inject", json={"message": "too late"})
        assert r.status_code == 409
        proc.end()

    def test_nothing_running_is_a_409(self, client) -> None:  # noqa: F811
        assert client.post("/sessions/sess-1/resume/inject", json={"message": "hi"}).status_code == 409

    def test_unknown_session_404(self, client) -> None:  # noqa: F811
        assert client.post("/sessions/nope/resume/inject", json={"message": "hi"}).status_code == 404


class TestStop:
    def test_stop_interrupts_and_pauses_the_queue(self, client, procs) -> None:  # noqa: F811
        proc = _start_turn(client, procs)
        client.post("/sessions/sess-1/resume/queue", json={"message": "deploy when done"})
        r = client.post("/sessions/sess-1/resume/stop").json()
        assert r["stopped"] is True and r["paused"] is True
        assert '"interrupt"' in proc.stdin.lines[-1]
        proc.emit(json.dumps({"type": "result", "subtype": "error_during_execution"}))
        proc.end()
        assert _wait(lambda: not _status(client)["running"])
        time.sleep(0.3)
        st = _status(client)
        assert st["paused"] is True and st["queue_count"] == 1 and len(procs) == 1  # waits

        client.post("/sessions/sess-1/resume/queue/run")
        assert _wait(lambda: len(procs) == 2)
        assert procs[1].stdin.wait_first() and procs[1].stdin.texts() == ["deploy when done"]
        procs[1].end()

    def test_stop_with_nothing_running(self, client) -> None:  # noqa: F811
        assert client.post("/sessions/sess-1/resume/stop").json()["stopped"] is False


class TestQueuedMessages:
    def _hold(self, monkeypatch) -> None:
        # The session works in a terminal tab, so queued messages wait.
        class Busy:
            active = True

            def as_dict(self):
                return {"open": True, "busy": True, "active": True, "agents": 0}

        monkeypatch.setattr(sessions_router, "_terminal", lambda session_id: Busy())

    def test_queued_items_have_ids(self, client, monkeypatch) -> None:  # noqa: F811
        self._hold(monkeypatch)
        r = client.post("/sessions/sess-1/resume/queue", json={"message": "one"}).json()
        items = _status(client)["queue_items"]
        assert items == [{"id": r["id"], "preview": "one", "text": "one"}]

    def test_delete_and_undo(self, client, monkeypatch) -> None:  # noqa: F811
        self._hold(monkeypatch)
        a = client.post("/sessions/sess-1/resume/queue", json={"message": "one"}).json()["id"]
        client.post("/sessions/sess-1/resume/queue", json={"message": "two"})
        r = client.delete(f"/sessions/sess-1/resume/queue/{a}")
        assert r.status_code == 200 and r.json()["position"] == 0
        assert [i["text"] for i in _status(client)["queue_items"]] == ["two"]
        assert client.post(f"/sessions/sess-1/resume/queue/{a}/restore").status_code == 200
        assert [i["text"] for i in _status(client)["queue_items"]] == ["one", "two"]
        assert client.delete("/sessions/sess-1/resume/queue/nope").status_code == 404

    def test_edit_keeps_the_files(self, client, monkeypatch) -> None:  # noqa: F811
        self._hold(monkeypatch)
        b64 = "aGVsbG8="
        a = client.post(
            "/sessions/sess-1/resume/queue", json={"message": "read this", "files": [{"name": "n.md", "data": b64}]}
        ).json()["id"]
        assert client.patch(f"/sessions/sess-1/resume/queue/{a}", json={"message": "summarize it"}).status_code == 200
        item = sessions_router._queues["sess-1"][0]
        assert item["text"] == "summarize it" and item["preview"] == "summarize it"
        assert item["message"].startswith("summarize it") and "n.md" in item["message"]
        assert client.patch(f"/sessions/sess-1/resume/queue/{a}", json={"message": " "}).status_code == 422

    def test_send_now_goes_into_the_running_turn(self, client, procs) -> None:  # noqa: F811
        proc = _start_turn(client, procs)
        a = client.post("/sessions/sess-1/resume/queue", json={"message": "and deploy"}).json()["id"]
        r = client.post(f"/sessions/sess-1/resume/queue/{a}/send-now").json()
        assert r["status"] == "sent_mid_turn"
        assert proc.stdin.texts()[-1] == "and deploy"
        st = _status(client)
        assert st["queue_count"] == 0 and st["steers"][0]["id"] == a
        proc.end()

    def test_send_now_past_a_terminal_hold_starts_it(self, client, procs, monkeypatch) -> None:  # noqa: F811
        self._hold(monkeypatch)
        client.post("/sessions/sess-1/resume/queue", json={"message": "first"})
        b = client.post("/sessions/sess-1/resume/queue", json={"message": "urgent"}).json()["id"]
        time.sleep(0.2)
        assert procs == []  # held by the terminal
        r = client.post(f"/sessions/sess-1/resume/queue/{b}/send-now").json()
        assert r["status"] == "starting"
        assert _wait(lambda: procs and procs[0].stdin.lines)
        assert procs[0].stdin.texts() == ["urgent"]  # jumped the line
        procs[0].end()

    def test_send_all_now_sends_every_message_in_order(self, client, procs) -> None:  # noqa: F811
        proc = _start_turn(client, procs)
        client.post("/sessions/sess-1/resume/queue", json={"message": "one"})
        client.post("/sessions/sess-1/resume/queue", json={"message": "two"})
        r = client.post("/sessions/sess-1/resume/queue/send-all-now").json()
        assert [s["status"] for s in r["sent"]] == ["sent_mid_turn", "sent_mid_turn"]
        assert proc.stdin.texts()[-2:] == ["one", "two"]
        assert _status(client)["queue_count"] == 0
        proc.end()

    def test_send_now_on_a_missing_message_is_a_404(self, client) -> None:  # noqa: F811
        assert client.post("/sessions/sess-1/resume/queue/nope/send-now").status_code == 404
