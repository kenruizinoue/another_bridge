"""Card time and title come from what the conversation says, not from the
file (tickets 22 and 23).

On 2026-11-04 the phone showed "41 min ago" on a session last used two
days earlier: open Claude Code tabs touch their transcript files without
a new turn, so the file's mtime is not when Ken last used the session.
The same day a session named with ``-n`` / ``/rename`` still showed its
first auto title, because the index took the first ``ai-title`` and
ignored ``custom-title``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from services.session_index import SessionIndex

CWD = "/Users/dev/proj"


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _write(root: Path, session_id: str, events: list[dict], *, touch: float | None = None) -> Path:
    d = root / "-Users-dev-proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if touch is not None:
        os.utime(p, (touch, touch))
    return p


def _user(ts: str, text: str = "hi", **extra) -> dict:
    return {"type": "user", "cwd": CWD, "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def _assistant(ts: str, text: str = "ok", **extra) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _card(root: Path, session_id: str) -> dict:
    card = SessionIndex(projects_dir=root).get_card(session_id)
    assert card is not None
    return card


class TestLastActivity:
    def test_is_the_last_real_message_not_the_file_time(self, tmp_path: Path) -> None:
        # The file was touched just now, but the last message is from June.
        _write(tmp_path, "s1", [_user("2026-06-30T10:00:00Z"), _assistant("2026-06-30T10:00:03Z")], touch=time.time())
        assert _card(tmp_path, "s1")["last_activity"] == _epoch("2026-06-30T10:00:03Z")

    def test_ignores_idle_writes_after_the_last_message(self, tmp_path: Path) -> None:
        events = [
            _user("2026-06-30T10:00:00Z"),
            _assistant("2026-06-30T10:00:03Z"),
            # Idle compaction, its summary turn, a subagent and metadata
            # lines are written later without Ken doing anything.
            {"type": "system", "subtype": "informational", "timestamp": "2026-07-01T09:00:00Z",
             "content": "Compacted while idle"},
            _user("2026-07-01T09:00:01Z", "summary", isCompactSummary=True),
            _user("2026-07-01T09:00:02Z", "caveat", isMeta=True),
            _assistant("2026-07-01T09:00:03Z", "subagent", isSidechain=True),
            {"type": "last-prompt", "lastPrompt": "hi"},
            {"type": "ai-title", "aiTitle": "Some title"},
            {"type": "permission-mode", "permissionMode": "bypassPermissions"},
        ]
        _write(tmp_path, "s1", events, touch=time.time())
        assert _card(tmp_path, "s1")["last_activity"] == _epoch("2026-06-30T10:00:03Z")

    def test_a_tool_result_counts_as_activity(self, tmp_path: Path) -> None:
        # A long tool step is real work: its result lands as a user event.
        result = {
            "type": "user",
            "timestamp": "2026-06-30T10:05:00Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "done"}]},
        }
        _write(tmp_path, "s1", [_user("2026-06-30T10:00:00Z"), result])
        assert _card(tmp_path, "s1")["last_activity"] == _epoch("2026-06-30T10:05:00Z")

    def test_falls_back_to_the_file_time_without_timestamps(self, tmp_path: Path) -> None:
        events = [{"type": "user", "cwd": CWD, "message": {"role": "user", "content": "hi"}}]
        _write(tmp_path, "s1", events, touch=1_700_000_000.0)
        assert _card(tmp_path, "s1")["last_activity"] == 1_700_000_000.0

    def test_list_is_sorted_by_last_real_message(self, tmp_path: Path) -> None:
        # "old" was touched by an open tab a second ago; "new" was really
        # used later. "new" must come first.
        _write(tmp_path, "old", [_user("2026-06-01T10:00:00Z")], touch=time.time())
        _write(tmp_path, "new", [_user("2026-06-02T10:00:00Z")], touch=time.time() - 3600)
        cards, _ = SessionIndex(projects_dir=tmp_path).list_cards()
        assert [c["session_id"] for c in cards] == ["new", "old"]


class TestTitle:
    def test_a_custom_title_wins_over_the_ai_title(self, tmp_path: Path) -> None:
        events = [
            _user("2026-06-30T10:00:00Z", "name this session x"),
            {"type": "ai-title", "aiTitle": "Auto title"},
            {"type": "custom-title", "customTitle": "2026/Nov/4 AnotherBridge"},
            {"type": "ai-title", "aiTitle": "Auto title again"},
        ]
        _write(tmp_path, "s1", events)
        assert _card(tmp_path, "s1")["title"] == "2026/Nov/4 AnotherBridge"

    def test_the_latest_custom_title_wins(self, tmp_path: Path) -> None:
        events = [
            _user("2026-06-30T10:00:00Z"),
            {"type": "custom-title", "customTitle": "First name"},
            {"type": "custom-title", "customTitle": "Renamed"},
        ]
        _write(tmp_path, "s1", events)
        assert _card(tmp_path, "s1")["title"] == "Renamed"

    def test_the_latest_ai_title_wins(self, tmp_path: Path) -> None:
        events = [
            _user("2026-06-30T10:00:00Z"),
            {"type": "ai-title", "aiTitle": "Early guess"},
            _assistant("2026-06-30T10:00:03Z"),
            {"type": "ai-title", "aiTitle": "Better title"},
        ]
        _write(tmp_path, "s1", events)
        assert _card(tmp_path, "s1")["title"] == "Better title"

    def test_a_blank_custom_title_is_ignored(self, tmp_path: Path) -> None:
        events = [
            _user("2026-06-30T10:00:00Z"),
            {"type": "ai-title", "aiTitle": "Auto title"},
            {"type": "custom-title", "customTitle": "   "},
        ]
        _write(tmp_path, "s1", events)
        assert _card(tmp_path, "s1")["title"] == "Auto title"
