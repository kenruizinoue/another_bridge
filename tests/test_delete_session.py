"""Deleting a session from the phone (ticket 28): a critical action, so
the request carries the id twice, a busy or open session is refused, and
nothing is erased (it moves to a trash folder)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services.session_index import SessionIndex


def _write(root: Path, session_id: str) -> Path:
    d = root / "projects" / "-Users-dev"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text(
        json.dumps({"type": "user", "cwd": "/Users/dev", "timestamp": "2026-06-30T10:00:00Z",
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    (d / session_id / "subagents").mkdir(parents=True)
    (d / session_id / "subagents" / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
    return p


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = _write(tmp_path, "s-del")
    monkeypatch.setattr(sessions_router, "session_index", SessionIndex(projects_dir=tmp_path / "projects"))
    monkeypatch.setattr(sessions_router, "_open_elsewhere", lambda session_id: False)
    att = sessions_router.ANOTHER_CODER_ATTACHMENTS_DIR / "s-del"
    att.mkdir(parents=True)
    (att / "x.md").write_text("x", encoding="utf-8")
    app = FastAPI()
    app.include_router(sessions_router.router)
    yield TestClient(app), path
    with sessions_router._running_guard:
        sessions_router._running_resumes.clear()
    with sessions_router._queue_guard:
        sessions_router._queues.clear()


def _delete(client, session_id, confirm):
    return client.request("DELETE", f"/sessions/{session_id}", json={"confirm": confirm})


def test_moves_the_session_to_the_trash(setup) -> None:
    client, path = setup
    r = _delete(client, "s-del", "s-del")
    assert r.status_code == 200
    body = r.json()
    trash = Path(body["trash"])
    assert sorted(body["moved"]) == ["attachments", "s-del.jsonl", "session_folder"]
    assert not path.exists() and not path.with_suffix("").exists()
    assert (trash / "s-del.jsonl").exists()
    assert (trash / "session_folder" / "subagents" / "agent-1.jsonl").exists()
    assert (trash / "attachments" / "x.md").exists()
    assert (trash / "origin.txt").read_text().strip() == str(path)
    assert client.get("/sessions/s-del").status_code == 404


def test_the_confirm_must_be_the_id(setup) -> None:
    client, path = setup
    assert _delete(client, "s-del", "something-else").status_code == 422
    assert client.request("DELETE", "/sessions/s-del").status_code == 422  # no body
    assert path.exists()


def test_unknown_session_404(setup) -> None:
    client, _ = setup
    assert _delete(client, "nope", "nope").status_code == 404


def test_refused_while_a_phone_turn_runs_or_messages_wait(setup) -> None:
    client, path = setup
    with sessions_router._running_guard:
        sessions_router._running_resumes["s-del"] = time.time()
    assert _delete(client, "s-del", "s-del").status_code == 409
    with sessions_router._running_guard:
        sessions_router._running_resumes.clear()
    with sessions_router._queue_guard:
        sessions_router._queues["s-del"] = [{"preview": "x", "message": "x", "images": []}]
    assert _delete(client, "s-del", "s-del").status_code == 409
    assert path.exists()


def test_refused_while_open_in_a_mac_tab(setup, monkeypatch) -> None:
    client, path = setup
    monkeypatch.setattr(sessions_router, "_open_elsewhere", lambda session_id: True)
    r = _delete(client, "s-del", "s-del")
    assert r.status_code == 409 and "Mac tab" in r.json()["detail"]
    assert path.exists()
