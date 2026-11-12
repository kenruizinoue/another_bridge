"""Phone-made sessions listed by Claude Code's /resume (ticket 33).

Claude Code hides a session from /resume when the first ``entrypoint`` in
the first 64 KB of its transcript is ``sdk-cli`` (a ``claude -p`` turn,
so every session the phone starts). ``claude_sees`` below ports that check
from Claude Code 2.1.319 so the tests prove the marked file passes it.
Fake transcripts only; nothing touches the real ~/.claude.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routers import sessions as sessions_router
from services import resume_listing


def claude_sees(path: Path) -> str | None:
    """Claude Code's check: the first ``"entrypoint":"`` (or with a space
    after the colon) in the first 64 KB, up to the next quote."""
    head = path.read_bytes()[:65536].decode("utf-8", "replace")
    for key in ('"entrypoint":"', '"entrypoint": "'):
        i = head.find(key)
        if i < 0:
            continue
        start = i + len(key)
        end = head.find('"', start)
        if end >= 0:
            return head[start:end]
    return None


def _line(**fields) -> str:
    return json.dumps(fields, separators=(",", ":"))


def _phone_session(path: Path) -> Path:
    """What a session started from the phone looks like: header lines
    without an entrypoint, then turns written by ``claude -p``."""
    lines = [
        _line(type="custom-title", customTitle="2026/Nov/11 AnotherStorage", sessionId="s1"),
        _line(type="queue-operation", operation="enqueue", sessionId="s1"),
        _line(parentUuid=None, type="user", userType="external", entrypoint="sdk-cli", cwd="/Users/k",
              message={"role": "user", "content": "start the storage idea"}),
        _line(type="assistant", entrypoint="sdk-cli", message={"content": [{"type": "text", "text": "ok"}]}),
        _line(type="user", entrypoint="sdk-cli", message={"role": "user", "content": "second phone turn"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- the file


def test_marks_a_phone_session_so_resume_lists_it(tmp_path: Path) -> None:
    path = _phone_session(tmp_path / "s1.jsonl")
    before = path.read_bytes()
    assert claude_sees(path) == "sdk-cli"

    assert resume_listing.show_in_resume(path) == resume_listing.SHOWN

    after = path.read_bytes()
    assert claude_sees(path) == "cli"
    assert resume_listing.first_entrypoint(path) == "cli"
    assert len(after) == len(before)
    rows = [json.loads(line) for line in after.decode().splitlines()]
    assert [r.get("entrypoint") for r in rows] == [None, None, "cli", "sdk-cli", "sdk-cli"]
    assert rows[2]["message"]["content"] == "start the storage idea"


def test_a_terminal_session_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(_line(type="user", entrypoint="cli") + "\n" + _line(type="user", entrypoint="sdk-cli") + "\n")
    before = path.read_bytes()
    assert resume_listing.show_in_resume(path) == resume_listing.ALREADY
    assert path.read_bytes() == before


def test_marking_twice_changes_nothing_more(tmp_path: Path) -> None:
    path = _phone_session(tmp_path / "s1.jsonl")
    resume_listing.show_in_resume(path)
    once = path.read_bytes()
    assert resume_listing.show_in_resume(path) == resume_listing.ALREADY
    assert path.read_bytes() == once


def test_no_value_in_the_first_64_kb_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    big = _line(type="user", message={"content": "x" * 70_000})
    path.write_text(big + "\n" + _line(type="user", entrypoint="sdk-cli") + "\n")
    before = path.read_bytes()
    assert resume_listing.first_entrypoint(path) is None
    assert resume_listing.show_in_resume(path) == resume_listing.UNKNOWN
    assert path.read_bytes() == before


def test_lines_appended_around_the_mark_survive(tmp_path: Path) -> None:
    path = _phone_session(tmp_path / "s1.jsonl")
    with open(path, "a", encoding="utf-8") as writer:  # claude keeps its file open in append mode
        writer.write(_line(type="assistant", entrypoint="sdk-cli", n=1) + "\n")
        writer.flush()
        resume_listing.show_in_resume(path)
        writer.write(_line(type="assistant", entrypoint="sdk-cli", n=2) + "\n")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r.get("n") for r in rows[-2:]] == [1, 2]
    assert claude_sees(path) == "cli"


# ---------------------------------------------------- after a phone turn


@pytest.fixture
def phone_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _phone_session(tmp_path / "s1.jsonl")
    monkeypatch.setattr(sessions_router.session_index, "transcript_path", lambda sid: path if sid == "s1" else None)
    monkeypatch.setattr(sessions_router, "_phone_turn_pending", lambda sid: False)
    monkeypatch.setattr(sessions_router, "_open_elsewhere", lambda sid: False)
    return path


def test_the_end_of_a_phone_turn_marks_the_session(phone_session: Path) -> None:
    sessions_router._end_resume("s1")
    assert claude_sees(phone_session) == "cli"


def test_waits_while_a_message_is_queued(phone_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_router, "_phone_turn_pending", lambda sid: True)
    sessions_router._end_resume("s1")
    assert claude_sees(phone_session) == "sdk-cli"


def test_waits_while_a_mac_tab_holds_it(phone_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_router, "_open_elsewhere", lambda sid: True)
    sessions_router._end_resume("s1")
    assert claude_sees(phone_session) == "sdk-cli"


def test_a_terminal_session_never_scans_for_holders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(_line(type="user", entrypoint="cli") + "\n")
    monkeypatch.setattr(sessions_router.session_index, "transcript_path", lambda sid: path)

    def no_scan(_sid):
        raise AssertionError("scanned for holders")

    monkeypatch.setattr(sessions_router, "_open_elsewhere", no_scan)
    sessions_router._end_resume("s1")


def test_a_missing_transcript_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_router.session_index, "transcript_path", lambda sid: None)
    sessions_router._end_resume("gone")


def test_a_failure_never_breaks_the_end_of_a_turn(phone_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr(sessions_router.resume_listing, "show_in_resume", boom)
    sessions_router._end_resume("s1")
    assert claude_sees(phone_session) == "sdk-cli"
