"""Open a session in a new terminal tab on the Mac (ticket 25).

The phone's "Open on Mac" button lands here: the bridge opens a new iTerm2
tab (or a Terminal window when iTerm2 isn't installed) that runs
``claude --resume <id>`` in the session's folder, so Ken walks to the
laptop and the conversation is already open.

The command is typed into the new tab's shell (iTerm2 ``write text``,
Terminal ``do script``) instead of replacing the shell, so PATH and
aliases from Ken's shell apply and the tab stays open after claude exits.

Nothing here decides WHEN to open: the router checks that no phone turn
is writing the session first, so two copies never write one transcript.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

TERMINAL_APPS = ("auto", "iterm", "terminal")
ITERM_APP_PATH = Path("/Applications/iTerm.app")
OSASCRIPT_TIMEOUT_SECONDS = 20

# Claude Code session ids are UUIDs. Anything else never reaches a shell.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


class OpenError(Exception):
    """The tab could not be opened (bad input, or osascript failed)."""


def resume_command(session_id: str, cwd: str, *, skip_permissions: bool, claude: str = "claude") -> str:
    """The shell line the new tab runs. Both values are shell-quoted; a
    session id that isn't id-shaped, or a folder with control characters,
    is refused."""
    if not _SESSION_ID.match(session_id):
        raise OpenError(f"not a session id: {session_id!r}")
    if not cwd or any(ord(ch) < 32 for ch in cwd):
        raise OpenError("the session's folder is empty or has control characters")
    flags = " --dangerously-skip-permissions" if skip_permissions else ""
    return f"cd {shlex.quote(cwd)} && {claude}{flags} --resume {shlex.quote(session_id)}"


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def iterm_script(command: str) -> str:
    """A new tab in the front iTerm2 window (a new window when none is
    open), with ``command`` typed into its shell."""
    cmd = _applescript_string(command)
    return "\n".join(
        [
            'tell application "iTerm"',
            "    activate",
            "    if (count of windows) is 0 then",
            "        set w to (create window with default profile)",
            "    else",
            "        set w to current window",
            "        tell w to create tab with default profile",
            "    end if",
            f"    tell current session of w to write text {cmd}",
            "end tell",
        ]
    )


def terminal_script(command: str) -> str:
    """A new Terminal window running ``command`` in its shell."""
    cmd = _applescript_string(command)
    return "\n".join(
        [
            'tell application "Terminal"',
            "    activate",
            f"    do script {cmd}",
            "end tell",
        ]
    )


def pick_app(preference: str, *, iterm_installed: bool) -> str:
    """``iterm`` or ``terminal``. ``auto`` and ``iterm`` use iTerm2 when it
    is installed and fall back to Terminal, which every Mac has."""
    if preference not in TERMINAL_APPS:
        raise OpenError(f"unknown terminal app {preference!r} (use one of {', '.join(TERMINAL_APPS)})")
    if preference == "terminal":
        return "terminal"
    return "iterm" if iterm_installed else "terminal"


def open_tab(
    command: str,
    app: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Run the AppleScript for ``app``. Raises OpenError with osascript's
    message when it fails, for example when macOS has not yet allowed the
    bridge to control the terminal app (Settings, Privacy and Security,
    Automation)."""
    script = iterm_script(command) if app == "iterm" else terminal_script(command)
    try:
        result = run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise OpenError(f"osascript failed: {err}") from err
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise OpenError(f"osascript exited {result.returncode}: {detail}")


def resume_processes(
    session_id: str,
    *,
    exclude_parent: int,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[int]:
    """Pids of processes started with ``--resume <session_id>``, other than
    children of ``exclude_parent`` (the bridge's own phone turns).

    Claude Code writes its ``sessions/<pid>.json`` record only after its
    startup questions, such as "trust this folder", which it asks in the
    home folder every time. A tab opened a moment ago that still waits on
    that question has no record yet, so without this check a second tap
    opened a second tab (2026-11-05). Errors fail open (an empty list)."""
    try:
        out = run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2].split()
        if ppid == exclude_parent:
            continue
        for flag, value in zip(args, args[1:]):
            if flag in ("--resume", "-r") and value == session_id:
                pids.append(pid)
                break
    return pids


def validate_config(terminal_app: str, new_session_cwd: Path) -> None:
    """Boot check: an unknown ANOTHER_BRIDGE_TERMINAL_APP or a missing
    ANOTHER_BRIDGE_NEW_SESSION_CWD stops the server instead of failing on
    the first tap from the phone."""
    if terminal_app not in TERMINAL_APPS:
        raise OpenError(
            f"ANOTHER_BRIDGE_TERMINAL_APP={terminal_app!r} is not one of {', '.join(TERMINAL_APPS)}"
        )
    if not new_session_cwd.is_dir():
        raise OpenError(f"ANOTHER_BRIDGE_NEW_SESSION_CWD={str(new_session_cwd)!r} is not a folder")
