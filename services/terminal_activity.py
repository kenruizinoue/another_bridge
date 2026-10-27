"""Is a session busy in another Claude Code process on this Mac?

Claude Code writes one small JSON file per running process to
``<claude-config>/sessions/<pid>.json``: the pid, the ``sessionId`` it
holds, its ``entrypoint`` ("cli" for a terminal tab) and a ``status`` of
"busy" or "idle". Ken keeps a terminal tab open on most sessions and also
continues them from the phone. A phone turn starts a second
``claude --resume`` on the same transcript, so when the terminal copy is
still working, the two copies redo the same work. On October 27, 2026 two
copies each wrote the GR00T reel script with their own reviewers, and the
phone copy restarted the terminal's reviewer agents a second time.

This module answers one question for the router: is another live copy of
the session working right now? It counts as working when its status is
"busy", or when its own lines (matched by its ``entrypoint``) or its
agents' transcripts changed within ``active_seconds``. "busy" alone is
not enough: a terminal whose turn ended stays "idle" while its background
agents keep running. The bridge's own ``claude`` children never count.

Anything unreadable fails open (no state), so a format change in Claude
Code degrades to the old behavior instead of a queue that never drains.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from config import (
    ANOTHER_BRIDGE_CLAUDE_SESSIONS_DIR,
    ANOTHER_BRIDGE_TERMINAL_ACTIVE_SECONDS,
)

log = structlog.get_logger()

# How much of a transcript's end to read when looking for the other
# copy's newest line. Big sessions are hundreds of MB; the tail is enough.
TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class TerminalState:
    """Another live process holding the session (usually a terminal tab)."""

    pid: int
    entrypoint: str
    busy: bool  # its status file says "busy" (mid-turn)
    active: bool  # busy, or its lines or agents changed within the window
    agents: int  # its agent transcripts that changed within the window

    def as_dict(self) -> dict:
        return {"open": True, "busy": self.busy, "active": self.active, "agents": self.agents}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _parent_pids(pids: list[int]) -> dict[int, int]:
    """pid -> parent pid via one ``ps`` call. Empty on any failure."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,ppid=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    parents: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def _epoch(ts: object) -> Optional[float]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    """The last whole lines of a file, newest last ([] if unreadable)."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="ignore").splitlines()
    return lines[1:] if start > 0 else lines  # the first may be cut


def _newest_line_time(path: Path, entrypoint: str) -> Optional[float]:
    """Timestamp of the newest line ``entrypoint`` wrote to ``path``."""
    for raw in reversed(_tail_lines(path)):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("entrypoint") == entrypoint:
            stamp = _epoch(event.get("timestamp"))
            if stamp is not None:
                return stamp
    return None


class TerminalWatch:
    """Reads Claude Code's per-process files. One instance per bridge."""

    def __init__(
        self,
        sessions_dir: Path,
        active_seconds: int = ANOTHER_BRIDGE_TERMINAL_ACTIVE_SECONDS,
        own_pid: Optional[int] = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.active_seconds = active_seconds
        self._own_pid = own_pid
        self._warned = False

    def records(self) -> dict[str, dict]:
        """sessionId -> the live record of another process holding it.
        Skips dead pids and the bridge's own ``claude`` children. When a
        session has several holders, a busy one wins."""
        try:
            files = list(self.sessions_dir.glob("*.json"))
        except OSError as err:
            self._warn(err)
            return {}
        live: list[dict] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            pid, sid = data.get("pid"), data.get("sessionId")
            if isinstance(pid, int) and isinstance(sid, str) and _pid_alive(pid):
                live.append(data)
        own = self._own_pid if self._own_pid is not None else os.getpid()
        parents = _parent_pids([d["pid"] for d in live])
        found: dict[str, dict] = {}
        for data in live:
            if data["pid"] == own or parents.get(data["pid"]) == own:
                continue  # the bridge itself, or a phone turn it started
            current = found.get(data["sessionId"])
            if current is None or (data.get("status") == "busy" and current.get("status") != "busy"):
                found[data["sessionId"]] = data
        return found

    def state(
        self,
        session_id: str,
        transcript: Optional[Path],
        records: Optional[dict[str, dict]] = None,
        now: Optional[float] = None,
    ) -> Optional[TerminalState]:
        """The other copy's state, or None when nothing else holds the
        session (or anything failed: fail open)."""
        try:
            rec = (self.records() if records is None else records).get(session_id)
            if rec is None:
                return None
            now = time.time() if now is None else now
            entrypoint = str(rec.get("entrypoint") or "cli")
            busy = rec.get("status") == "busy"
            recent = False
            agents = 0
            if transcript is not None:
                newest = _newest_line_time(transcript, entrypoint)
                recent = newest is not None and now - newest <= self.active_seconds
                agents = self._active_agents(transcript, entrypoint, now)
            return TerminalState(
                pid=int(rec["pid"]), entrypoint=entrypoint, busy=busy,
                active=busy or recent or agents > 0, agents=agents,
            )
        except Exception as err:  # fail open, never break a send
            self._warn(err)
            return None

    def _active_agents(self, transcript: Path, entrypoint: str, now: float) -> int:
        """Agent transcripts the other copy changed within the window. An
        agent's lines carry the entrypoint of the process running it."""
        folder = transcript.parent / transcript.stem / "subagents"
        count = 0
        try:
            candidates = list(folder.glob("*.jsonl"))
        except OSError:
            return 0
        for f in candidates:
            try:
                if now - f.stat().st_mtime > self.active_seconds:
                    continue
            except OSError:
                continue
            newest = _newest_line_time(f, entrypoint)
            if newest is not None and now - newest <= self.active_seconds:
                count += 1
        return count

    def _warn(self, err: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("terminal_activity.unreadable", error=str(err), dir=str(self.sessions_dir))


watch = TerminalWatch(ANOTHER_BRIDGE_CLAUDE_SESSIONS_DIR)
