#!/usr/bin/env python3
"""Claude Code hook: catch a terminal tab up on turns sent from the phone.

A phone turn runs as a separate ``claude --resume`` process (entrypoint
"sdk-cli") and appends to the session's transcript. The terminal tab
holding the same session never re-reads that file, so without this it
doesn't know what the phone did and can redo or undo that work. Ken used
to type /sync-mobile by hand for this.

Registered as a ``UserPromptSubmit`` hook in ``~/.claude/settings.json``.
Before each prompt typed in a terminal tab, it finds the phone turns that
tab hasn't seen (newer than when the tab started, and newer than the last
catch-up), and hands them to Claude as context, like /sync-mobile does.
It prints nothing when there are none, and nothing in a phone copy itself.

It must never block a prompt: every failure exits 0 with no output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
SESSIONS_DIR = CLAUDE_DIR / "sessions"
STATE_DIR = Path.home() / ".another_coder" / "terminal_sync"
IMG_DIR = Path("/tmp/sync-mobile-images")  # same folder /sync-mobile uses
PHONE_ENTRYPOINT = "sdk-cli"
TAIL_BYTES = 4 * 1024 * 1024
MAX_TURNS = 30
MAX_TURN_CHARS = 2000
SKIP_PREFIXES = ("<command-", "Caveat:", "<local-command", "<task-notification>", "<system-reminder>")
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif"}

HEADER = (
    "These turns were added to this session from Ken's phone (another_bridge) "
    "after this terminal tab last saw the conversation. Treat them as part of "
    "the conversation, in order, and don't redo or undo their work. Don't "
    "summarize them back unless asked."
)


def _epoch(ts: object) -> Optional[float]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_process(start_pid: int, sessions_dir: Path = SESSIONS_DIR) -> Optional[dict]:
    """The Claude Code process this hook runs under: walk up the parent
    chain to the pid that has a ``sessions/<pid>.json`` record."""
    pid = start_pid
    for _ in range(8):
        record = sessions_dir / f"{pid}.json"
        if record.is_file():
            try:
                return json.loads(record.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
            pid = int(out.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if pid <= 1:
            return None
    return None


def _tail(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        fh.seek(start)
        lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    return lines[1:] if start > 0 else lines


def phone_turns(transcript: Path, since: float) -> list[tuple[float, str, str, list]]:
    """(time, who, text, images) for phone-written turns newer than ``since``."""
    turns = []
    for raw in _tail(transcript):
        if PHONE_ENTRYPOINT not in raw:
            continue  # cheap filter before parsing big lines
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("entrypoint") != PHONE_ENTRYPOINT:
            continue
        if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        stamp = _epoch(d.get("timestamp"))
        if stamp is None or stamp <= since:
            continue
        content = (d.get("message") or {}).get("content")
        text, images = "", []
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    src = block.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        images.append((src.get("media_type", "image/png"), src["data"]))
            text = " ".join(parts)
        text = text.strip()
        if (not text and not images) or text.startswith(SKIP_PREFIXES):
            continue
        turns.append((stamp, "You" if d["type"] == "user" else "Claude", text, images))
    return turns


def render(turns: list[tuple[float, str, str, list]], img_dir: Path = IMG_DIR) -> str:
    """The context block, like /sync-mobile's: newest turns, images saved
    to files with a list to Read."""
    lines = [HEADER, ""]
    if len(turns) > MAX_TURNS:
        lines.append(f"({len(turns) - MAX_TURNS} earlier phone turns left out)")
        turns = turns[-MAX_TURNS:]
    saved = []
    for _, who, text, images in turns:
        if len(text) > MAX_TURN_CHARS:
            text = text[:MAX_TURN_CHARS] + " [cut]"
        lines.append(f"[{who}] {text}" if text else f"[{who}] (image only)")
        for media_type, data in images:
            try:
                blob = base64.b64decode(data)
            except (ValueError, TypeError):
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            path = img_dir / f"{hashlib.sha1(blob).hexdigest()[:12]}.{EXT.get(media_type, 'png')}"
            if not path.exists():
                path.write_bytes(blob)
            saved.append(str(path))
            lines.append(f"    [attached image: {path}]")
    if saved:
        lines += ["", "IMAGES_TO_READ (Read each so you can see them):", *saved]
    return "\n".join(lines)


def _marker(state_dir: Path, pid: int, session_id: str) -> Path:
    return state_dir / f"{pid}-{session_id}.json"


def catch_up(
    session_id: str, transcript: Path, process: dict, state_dir: Path = STATE_DIR, img_dir: Path = IMG_DIR
) -> Optional[str]:
    """The context to add before this prompt, or None. Records how far the
    tab has seen, so each phone turn is handed over once."""
    if process.get("entrypoint") != "cli" or process.get("sessionId") != session_id:
        return None  # a phone copy, the SDK, or another session's process
    pid = int(process["pid"])
    started = float(process.get("startedAt") or 0) / 1000  # turns before it were loaded at start
    marker = _marker(state_dir, pid, session_id)
    since = started
    try:
        since = max(since, float(json.loads(marker.read_text(encoding="utf-8"))["since"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    turns = phone_turns(transcript, since)
    if not turns:
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"since": turns[-1][0], "updated": time.time()}), encoding="utf-8")
    return render(turns, img_dir)


def mark_seen(session_id: str, process: dict, state_dir: Path = STATE_DIR, now: Optional[float] = None) -> None:
    """/sync-mobile already showed the tab everything up to now."""
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    _marker(state_dir, int(process["pid"]), session_id).write_text(
        json.dumps({"since": stamp, "updated": stamp}), encoding="utf-8"
    )


def prune(state_dir: Path = STATE_DIR) -> None:
    """Drop markers of processes that have exited."""
    for marker in state_dir.glob("*.json") if state_dir.is_dir() else []:
        pid = marker.name.split("-", 1)[0]
        if not pid.isdigit():
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--mark-seen":
            process = find_process(os.getppid())
            if process and process.get("sessionId"):
                mark_seen(process["sessionId"], process)
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = payload.get("session_id")
        transcript = payload.get("transcript_path")
        if not session_id or not transcript or not Path(transcript).is_file():
            return 0
        process = find_process(os.getppid())
        if process is None:
            return 0
        context = catch_up(session_id, Path(transcript), process)
        prune()
        if context:
            print(json.dumps({
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}
            }))
    except Exception:  # never block or break a prompt
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
