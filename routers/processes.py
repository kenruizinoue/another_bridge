"""The phone's Processes view (ticket 29): every live Claude Code process
on the Mac (terminal tabs and phone turns) with the Bash commands running
under it, and a way to stop one of those commands.

Phone key only, like /sessions. Stopping is limited to Bash commands of a
live Claude process (services/process_view.stoppable), and the request
repeats the pid as a second check.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import ANOTHER_CODER_RATE_LIMIT_SESSIONS
from services import process_view, terminal_activity
from services.rate_limiter import limiter
from services.session_index import session_index

router = APIRouter()
log = structlog.get_logger()

STOP_GRACE_SECONDS = 3.0


def _records() -> list[dict]:
    """Every Claude Code process record, the bridge's own turns included."""
    return process_view.read_records(terminal_activity.watch.sessions_dir)


@router.get("/processes")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def list_processes(request: Request) -> dict[str, Any]:
    """``claude``: one entry per live Claude process (``origin`` phone,
    mac_tab or headless; ``status``; ``elapsed`` seconds; ``title`` of its
    session) with its running Bash ``commands`` (pid, redacted command,
    elapsed, cpu, processes). ``commands``: the total count."""
    entries = process_view.snapshot(_records(), process_view.read_ps(), os.getpid())
    titles: dict[str, str] = {}
    for e in entries:
        sid = e.get("session_id")
        if sid and sid not in titles:
            card = session_index.get_card(sid)
            titles[sid] = card["title"] if card else sid
        e["title"] = titles.get(sid or "", "Claude")
    return {"claude": entries, "commands": sum(len(e["commands"]) for e in entries)}


class StopRequest(BaseModel):
    confirm: int


def _end_group(pgid: int, pid: int) -> None:
    """SIGTERM the command's process group, SIGKILL after a grace."""
    process_view.end_group(pgid, STOP_GRACE_SECONDS)


@router.post("/processes/{pid}/stop")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def stop_process(request: Request, pid: int, body: StopRequest) -> dict[str, Any]:
    """Stop one Bash command a Claude process runs (and what it started).
    The Claude process gets the usual "command was stopped" result and
    keeps going. 422 when ``confirm`` isn't the pid, 404 when the pid is
    not such a command (already ended, or not allowed)."""
    if body.confirm != pid:
        raise HTTPException(status_code=422, detail="confirm must be the pid")
    proc = process_view.stoppable(pid, _records(), process_view.read_ps())
    if proc is None:
        raise HTTPException(status_code=404, detail="that command is not running any more")
    # The Bash tool gives each command its own group; never signal the
    # Claude process's own group.
    if proc.pgid == proc.pid:
        _end_group(proc.pgid, proc.pid)
    else:
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    log.warning("process.stopped", pid=pid, pgid=proc.pgid, command=process_view.redact(
        process_view.bash_command(proc.command) or "")[:120])
    return {"ok": True, "pid": pid}
