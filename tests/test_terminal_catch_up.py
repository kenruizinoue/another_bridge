"""The terminal catch-up hook (scripts/terminal_catch_up.py).

A terminal tab never re-reads its transcript, so phone turns appended by
another_bridge are invisible to it. Before each typed prompt the hook hands
the tab the phone turns it hasn't seen, once. Fake transcripts and process
records only; nothing reads the real ~/.claude.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "terminal_catch_up.py"
spec = importlib.util.spec_from_file_location("terminal_catch_up", SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

SID = "sess-1"
STARTED = time.time() - 3600  # the tab started an hour ago


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _line(entrypoint: str, kind: str, content, at: float, **extra) -> str:
    return json.dumps({
        "type": kind, "entrypoint": entrypoint, "timestamp": _iso(at), "sessionId": SID,
        "message": {"role": kind, "content": content}, **extra,
    })


@pytest.fixture
def tab() -> dict:
    return {"pid": 4242, "sessionId": SID, "entrypoint": "cli", "startedAt": STARTED * 1000}


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    now = time.time()
    lines = [
        _line("sdk-cli", "user", "old phone turn, loaded when the tab started", STARTED - 60),
        _line("cli", "user", "typed in the terminal", now - 600),
        _line("sdk-cli", "user", "still waiting?", now - 300),
        _line("sdk-cli", "assistant", [{"type": "text", "text": "Yes, on the designer."}], now - 290),
        _line("sdk-cli", "user", [{"type": "tool_result", "content": "x"}], now - 280),  # tool noise
        _line("sdk-cli", "user", "<task-notification>done</task-notification>", now - 270),
        _line("sdk-cli", "assistant", [{"type": "text", "text": "agent text"}], now - 260, isSidechain=True),
    ]
    path = tmp_path / "proj" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_hands_over_unseen_phone_turns_once(tmp_path: Path, transcript: Path, tab: dict) -> None:
    state = tmp_path / "state"
    context = hook.catch_up(SID, transcript, tab, state_dir=state, img_dir=tmp_path / "img")
    assert context is not None
    assert "[You] still waiting?" in context
    assert "[Claude] Yes, on the designer." in context
    assert "old phone turn" not in context  # before the tab started
    assert "typed in the terminal" not in context  # the tab's own turn
    assert "task-notification" not in context and "agent text" not in context

    # Next prompt: nothing new, nothing added.
    assert hook.catch_up(SID, transcript, tab, state_dir=state, img_dir=tmp_path / "img") is None

    # A later phone turn is handed over on its own.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_line("sdk-cli", "user", "build the reel", time.time()) + "\n")
    later = hook.catch_up(SID, transcript, tab, state_dir=state, img_dir=tmp_path / "img")
    assert "[You] build the reel" in later and "still waiting?" not in later


def test_phone_copies_and_other_sessions_get_nothing(tmp_path: Path, transcript: Path, tab: dict) -> None:
    phone = {**tab, "entrypoint": "sdk-cli"}
    assert hook.catch_up(SID, transcript, phone, state_dir=tmp_path) is None
    other = {**tab, "sessionId": "another"}
    assert hook.catch_up(SID, transcript, other, state_dir=tmp_path) is None


def test_sync_mobile_by_hand_counts_as_seen(tmp_path: Path, transcript: Path, tab: dict) -> None:
    hook.mark_seen(SID, tab, state_dir=tmp_path)
    assert hook.catch_up(SID, transcript, tab, state_dir=tmp_path) is None


def test_phone_images_are_saved_and_listed(tmp_path: Path, tab: dict) -> None:
    blob = b"\x89PNG fake"
    content = [
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(blob).decode()}},
    ]
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(_line("sdk-cli", "user", content, time.time()) + "\n", encoding="utf-8")
    context = hook.catch_up(SID, path, tab, state_dir=tmp_path / "state", img_dir=tmp_path / "img")
    assert "IMAGES_TO_READ" in context
    saved = list((tmp_path / "img").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == blob


def test_long_backlogs_are_capped(tmp_path: Path, tab: dict) -> None:
    now = time.time()
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(
        "\n".join(_line("sdk-cli", "user", f"msg {i} " + "x" * 3000, now - 100 + i) for i in range(40)) + "\n",
        encoding="utf-8",
    )
    context = hook.catch_up(SID, path, tab, state_dir=tmp_path / "state")
    assert "(10 earlier phone turns left out)" in context
    assert "msg 0 " not in context and "msg 39 " in context
    assert "[cut]" in context


def test_find_process_walks_up_to_the_claude_record(tmp_path: Path) -> None:
    import os

    (tmp_path / f"{os.getppid()}.json").write_text(json.dumps({"pid": os.getppid(), "sessionId": SID}))
    found = hook.find_process(os.getpid(), sessions_dir=tmp_path)
    assert found == {"pid": os.getppid(), "sessionId": SID}
    assert hook.find_process(os.getpid(), sessions_dir=tmp_path / "none") is None


def test_prune_drops_markers_of_exited_processes(tmp_path: Path) -> None:
    import os
    import subprocess

    proc = subprocess.Popen(["true"])
    proc.wait()
    (tmp_path / f"{proc.pid}-{SID}.json").write_text("{}")
    (tmp_path / f"{os.getpid()}-{SID}.json").write_text("{}")
    hook.prune(tmp_path)
    assert not (tmp_path / f"{proc.pid}-{SID}.json").exists()
    assert (tmp_path / f"{os.getpid()}-{SID}.json").exists()


def test_main_never_fails_a_prompt(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    monkeypatch.setattr("sys.argv", ["terminal_catch_up.py"])
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
