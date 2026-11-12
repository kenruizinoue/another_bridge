"""The open input of a running phone turn (ticket 26).

Every streamed phone turn runs ``claude -p --input-format stream-json``
with its stdin kept open, so the phone can:

- send a message INTO the running turn. Claude reads it at its next step,
  like typing into a busy terminal tab. Tested 2026-11-05: a message
  written while a 15 s command ran was read right after the command and
  the single reply included it.
- stop the turn with a ``control_request`` interrupt (same test: the turn
  ended at once with ``error_during_execution``).

``--replay-user-messages`` makes claude echo each user message it reads,
which is how a message sent mid-turn moves from ``sent`` to ``read``.
The router closes stdin when the turn's ``result`` arrives; claude then
exits. A message sent after that goes to the queue instead.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Optional


def user_line(message: str, images: list[dict]) -> str:
    """A stream-json user message: text plus image content blocks."""
    content: list[dict] = []
    if message:
        content.append({"type": "text", "text": message})
    for img in images:
        content.append({"type": "image", "source": {"type": "base64", **img}})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def interrupt_line() -> str:
    return json.dumps(
        {"type": "control_request", "request_id": f"stop-{uuid.uuid4().hex[:8]}", "request": {"subtype": "interrupt"}}
    ) + "\n"


def replayed_text(event: dict) -> Optional[str]:
    """The text of a user message claude echoed back, None for anything
    else (tool results come back as user events too, without text)."""
    if event.get("type") != "user":
        return None
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        return text or None
    return None


class TurnInput:
    """One running turn's stdin. All writes take the lock, so the first
    message, mid-turn messages, an interrupt and the close never
    interleave. Once closed (or the pipe broke) nothing more is written."""

    def __init__(self, stdin: Any) -> None:
        self._stdin = stdin
        self._lock = threading.Lock()
        self._open = stdin is not None
        self._steers: list[dict] = []
        # For the linger check: when claude last printed a line, and when
        # its last `result` came (None while a reply is being written).
        self._last_line = time.time()
        self._result_at: float | None = None

    @property
    def open(self) -> bool:
        with self._lock:
            return self._open

    def _write_locked(self, line: str) -> bool:
        if not self._open:
            return False
        try:
            self._stdin.write(line)
            self._stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError, AttributeError):
            self._open = False
            return False

    def send_first(self, message: str, images: list[dict]) -> bool:
        with self._lock:
            return self._write_locked(user_line(message, images))

    def steer(self, message: str, images: list[dict] | None = None, steer_id: str | None = None) -> Optional[dict]:
        """Write a message into the running turn. Returns its record
        (``id``, ``text``, ``state`` "sent"), or None when the turn can't
        take it any more (stdin closed: the turn is ending)."""
        with self._lock:
            if not self._open:
                return None
            entry = {"id": steer_id or uuid.uuid4().hex[:12], "text": message, "state": "sent", "sent_at": time.time()}
            # Registered before the write, so the echo can't arrive first.
            self._steers.append(entry)
            if not self._write_locked(user_line(message, images or [])):
                self._steers.remove(entry)
                return None
            return dict(entry)

    def interrupt(self) -> bool:
        with self._lock:
            return self._write_locked(interrupt_line())

    def mark_read(self, text: str) -> Optional[dict]:
        """Claude echoed a user message: the oldest matching sent one is
        now read. Returns its record, or None (the first message's echo,
        or anything else)."""
        with self._lock:
            for entry in self._steers:
                if entry["state"] == "sent" and entry["text"].strip() == text.strip():
                    entry["state"] = "read"
                    return dict(entry)
        return None

    def close(self) -> None:
        with self._lock:
            if self._open:
                self._open = False
                try:
                    self._stdin.close()
                except (OSError, ValueError, AttributeError):
                    pass

    def saw_line(self, is_result: bool, now: float | None = None) -> None:
        """Claude printed a line. A ``result`` marks the reply done; any
        other line (a background task's notice started a new turn) means
        it is working again."""
        stamp = time.time() if now is None else now
        with self._lock:
            self._last_line = stamp
            self._result_at = stamp if is_result else None

    def lingering(self, seconds: float, now: float | None = None) -> bool:
        """The reply is done and claude printed nothing for ``seconds``:
        the process only stays alive for something left in the background
        (a dev server, a watcher), and the session would wait on it."""
        stamp = time.time() if now is None else now
        with self._lock:
            return self._result_at is not None and stamp - self._last_line >= seconds

    def steers(self) -> list[dict]:
        with self._lock:
            return [{"id": e["id"], "text": e["text"], "state": e["state"]} for e in self._steers]


_inputs: dict[str, TurnInput] = {}
_guard = threading.Lock()


def register(session_id: str, inp: TurnInput) -> None:
    with _guard:
        _inputs[session_id] = inp


def get(session_id: str) -> Optional[TurnInput]:
    with _guard:
        return _inputs.get(session_id)


def drop(session_id: str) -> None:
    """The turn's claude exited: close its input and forget it."""
    with _guard:
        inp = _inputs.pop(session_id, None)
    if inp is not None:
        inp.close()
