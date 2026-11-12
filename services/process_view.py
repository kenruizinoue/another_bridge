"""What runs on the Mac, for the phone's Processes view (ticket 29).

Every live Claude Code process (a Mac terminal tab, a phone turn, or any
other headless run) has a ``sessions/<pid>.json`` record. Claude Code runs
each Bash command as its own ``zsh -c ... eval '<command>'`` child of that
process, in its own process group, so the commands of each session are
its children in ``ps``. This module turns one ``ps`` snapshot plus the
records into a list the phone can show, and checks that a pid the phone
asks to stop really is such a command (never a Claude process, never the
bridge).

Command lines can carry secrets (an API key in a curl), so they are
redacted before they leave the Mac.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    elapsed: int  # seconds
    cpu: float
    command: str


def parse_elapsed(text: str) -> int:
    """``ps`` etime, ``[[dd-]hh:]mm:ss``, as seconds (0 when malformed)."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d) if d.isdigit() else 0
    parts = [int(p) if p.isdigit() else 0 for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + s


def parse_ps(output: str) -> dict[int, Proc]:
    """Rows of ``ps -axo pid=,ppid=,pgid=,etime=,%cpu=,command=``."""
    rows: dict[int, Proc] = {}
    for line in output.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        try:
            cpu = float(parts[4])
        except ValueError:
            cpu = 0.0
        rows[int(parts[0])] = Proc(
            int(parts[0]), int(parts[1]), int(parts[2]), parse_elapsed(parts[3]), cpu, parts[5].strip()
        )
    return rows


def read_ps() -> dict[int, Proc]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,etime=,%cpu=,command="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ps(out)


# `... eval '<command>' < /dev/null && pwd -P >| ...`; a command with a
# heredoc has no `< /dev/null`. ps shows its newlines as \012.
_EVAL = re.compile(r"eval '(.*)'(?: < /dev/null)? && pwd -P")


def bash_command(command: str) -> Optional[str]:
    """The command Claude Code's Bash tool runs, out of its zsh wrapper
    (``... && eval '<command>' < /dev/null && pwd -P >| ...``), with the
    shell's ``'"'"'`` quote escapes undone. None for anything else."""
    if "shell-snapshots" not in command:
        return None
    m = _EVAL.search(command)
    if not m:
        return None
    return m.group(1).replace("'\"'\"'", "'").replace("\\012", "\n").strip()


_SECRETS = [
    (re.compile(r"\b((?:sk|pk|rk)_(?:live|test)_)[A-Za-z0-9]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1***"),
    (re.compile(r"((?:X-[A-Za-z-]*Key|Authorization)\s*:\s*)(?!Bearer\b)[^\s\"']+", re.IGNORECASE), r"\1***"),
    (
        re.compile(r"\b([A-Za-z_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Za-z_]*\s*=\s*)[^\s\"']+", re.IGNORECASE),
        r"\1***",
    ),
]


def redact(text: str) -> str:
    """Hide what looks like a key, token or password."""
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def _descendants(pid: int, children: dict[int, list[int]]) -> list[int]:
    out: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out


def snapshot(records: list[dict], rows: dict[int, Proc], bridge_pid: int) -> list[dict]:
    """One entry per live Claude process: where it runs (``phone`` = a
    child of the bridge, ``mac_tab`` = a terminal tab, ``headless``), its
    status, and the Bash commands running under it, oldest first."""
    children: dict[int, list[int]] = {}
    for p in rows.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out = []
    for rec in records:
        pid = rec.get("pid")
        proc = rows.get(pid) if isinstance(pid, int) else None
        if proc is None:
            continue
        if proc.ppid == bridge_pid:
            origin = "phone"
        elif rec.get("entrypoint") == "cli":
            origin = "mac_tab"
        else:
            origin = "headless"
        commands = []
        for child_pid in children.get(pid, []):
            child = rows[child_pid]
            text = bash_command(child.command)
            if text is None:
                continue
            under = _descendants(child_pid, children)
            commands.append(
                {
                    "pid": child.pid,
                    "command": redact(text),
                    "elapsed": child.elapsed,
                    "cpu": round(child.cpu + sum(rows[d].cpu for d in under), 1),
                    "processes": 1 + len(under),
                }
            )
        commands.sort(key=lambda c: -c["elapsed"])
        out.append(
            {
                "pid": pid,
                "session_id": rec.get("sessionId"),
                "origin": origin,
                "status": rec.get("status") or "unknown",
                "elapsed": proc.elapsed,
                "commands": commands,
            }
        )
    out.sort(key=lambda e: (-len(e["commands"]), e["origin"] != "phone", -e["elapsed"]))
    return out


def stoppable(pid: int, records: list[dict], rows: dict[int, Proc]) -> Optional[Proc]:
    """The command process for ``pid`` if it is a Bash command run by a
    live Claude process, else None. Claude processes themselves, the
    bridge and anything else on the Mac are never stoppable here."""
    claude_pids = {r.get("pid") for r in records if isinstance(r.get("pid"), int)}
    proc = rows.get(pid)
    if proc is None or proc.pid in claude_pids or proc.ppid not in claude_pids:
        return None
    return proc if bash_command(proc.command) is not None else None


def read_records(sessions_dir) -> list[dict]:
    """Every Claude Code process record (``sessions/<pid>.json``)."""
    import json

    out: list[dict] = []
    try:
        files = list(sessions_dir.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            out.append(data)
    return out


def end_group(pgid: int, grace: float = 3.0) -> None:
    """SIGTERM a command's process group, SIGKILL after ``grace``."""
    import os
    import signal
    import threading
    import time

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return

    def _later() -> None:
        time.sleep(grace)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    threading.Thread(target=_later, daemon=True).start()
