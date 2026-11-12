"""Let Claude Code's /resume list the sessions the phone started (ticket 33).

Claude Code hides a session from its /resume picker when the first
``entrypoint`` in the first 64 KB of the transcript is a background one
(``sdk-cli``). Every ``claude -p`` turn writes ``sdk-cli``, so every session
started from the phone was hidden, while sessions started in a terminal
and continued from the phone were listed. Claude reads that value with a
plain text search: the first ``"entrypoint":"`` and the characters up to
the next quote (checked in Claude Code 2.1.319).

``show_in_resume`` changes that one value to ``cli`` in place, padded with
spaces after the closing quote, so the line keeps its length and stays
valid JSON. Nothing else in the file moves, so a line Claude appends at the
same moment is safe. Only the first value changes; every later phone turn
keeps its ``sdk-cli``, which the terminal catch-up hook relies on.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

HEAD_BYTES = 64 * 1024  # what Claude Code reads for the check
_KEY = b'"entrypoint":"'
_HIDDEN = b'"entrypoint":"sdk-cli"'
_SHOWN = b'"entrypoint":"cli"'.ljust(len(_HIDDEN), b" ")

SHOWN = "shown"
ALREADY = "already"
UNKNOWN = "unknown"


def _first_value(head: bytes) -> Optional[tuple[int, bytes]]:
    """(offset, value) of the first ``"entrypoint":"<value>"`` in ``head``."""
    i = head.find(_KEY)
    if i < 0:
        return None
    start = i + len(_KEY)
    end = head.find(b'"', start)
    if end < 0:
        return None
    return i, head[start:end]


def first_entrypoint(path: Path) -> Optional[str]:
    """The entrypoint Claude Code's /resume check sees, or None when the
    first 64 KB hold none (Claude then falls back to the end of the file)."""
    with open(path, "rb") as f:
        found = _first_value(f.read(HEAD_BYTES))
    return found[1].decode("utf-8", "replace") if found else None


def show_in_resume(path: Path) -> str:
    """Mark the session as started in a terminal so /resume lists it.

    Returns ``shown`` when it changed the value, ``already`` when the first
    value isn't ``sdk-cli`` (nothing to do), ``unknown`` when the first
    64 KB hold no value in the expected form (left alone)."""
    with open(path, "r+b") as f:
        found = _first_value(f.read(HEAD_BYTES))
        if found is None:
            return UNKNOWN
        offset, value = found
        if value != b"sdk-cli":
            return ALREADY
        # Read the bytes again right before writing, so a file that changed
        # since the first read is never overwritten blindly.
        f.seek(offset)
        if f.read(len(_HIDDEN)) != _HIDDEN:
            return UNKNOWN
        f.seek(offset)
        f.write(_SHOWN)
        f.flush()
        os.fsync(f.fileno())
    return SHOWN
