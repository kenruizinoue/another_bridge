"""Live event log of each session's running turn, so a phone that left
the chat can come back and keep watching the reply stream in.

The resume paths publish a turn's ``text`` and ``tool`` events here as
claude prints them, from their reader thread, whether or not a phone is
connected. ``GET /sessions/{id}/resume/attach`` replays the message being
written now (finished ones are already in the transcript) and then
follows live until the turn ends. Only the running turn is kept, in
memory: it is created when a turn claims the session and dropped when
the claude process exits.
"""

import threading
from typing import Iterator


class LiveRun:
    """One turn's events, in order, plus where the current message starts."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        # Index of the first event of the message claude is writing now.
        # Earlier messages are already in the transcript, so a phone that
        # re-attaches after fetching /messages starts here.
        self.segment_start = 0
        self.done = False
        self._cond = threading.Condition()

    def publish(self, event: str, data: dict) -> None:
        with self._cond:
            self.events.append((event, data))
            self._cond.notify_all()

    def end_message(self) -> None:
        """A full assistant message landed, so it is in the transcript now."""
        with self._cond:
            self.segment_start = len(self.events)

    def finish(self) -> None:
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def follow(self, start: int | None = None, wait: float = 10.0) -> Iterator[tuple[str, dict] | None]:
        """Yield events from ``start`` (default: the current message's
        first one), then new ones as they arrive, until the turn finishes.
        Yields None after ``wait`` quiet seconds so callers can send a
        keepalive."""
        with self._cond:
            pos = self.segment_start if start is None else start
        while True:
            with self._cond:
                if pos >= len(self.events) and not self.done:
                    self._cond.wait(timeout=wait)
                batch = self.events[pos:]
                pos += len(batch)
                finished = self.done and pos >= len(self.events)
            if not batch and not finished:
                yield None
            yield from batch
            if finished:
                return


_runs: dict[str, LiveRun] = {}
_guard = threading.Lock()


def start(session_id: str) -> LiveRun:
    """A new turn claimed the session. Replaces any earlier run."""
    run = LiveRun()
    with _guard:
        _runs[session_id] = run
    return run


def get(session_id: str) -> LiveRun | None:
    """The running turn, or None when nothing is running."""
    with _guard:
        return _runs.get(session_id)


def finish(session_id: str) -> None:
    """The turn's claude process exited. Followers still holding the run
    read its last events and stop; new attaches see nothing running."""
    with _guard:
        run = _runs.pop(session_id, None)
    if run is not None:
        run.finish()
