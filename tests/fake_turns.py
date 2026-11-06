"""Fakes for phone turns, which run with an open stream-json stdin
(ticket 26). No real claude ever starts: conftest refuses
``streaming_subprocess_open`` unless a test installs one of these."""

from __future__ import annotations

import contextlib
import json
import threading


def message_text(line: str) -> str:
    """The text of a stream-json user line the bridge wrote."""
    try:
        data = json.loads(line)
    except ValueError:
        return ""
    content = (data.get("message") or {}).get("content") or []
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


class FakeStdin:
    def __init__(self, sink: list | None = None) -> None:
        self.lines: list[str] = []
        self.closed = False
        self._sink = sink
        self._first = threading.Event()

    def write(self, data: str) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self.lines.append(data)
        if len(self.lines) == 1:
            if self._sink is not None:
                self._sink.append(message_text(data))
            self._first.set()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def wait_first(self, timeout: float = 2.0) -> str:
        self._first.wait(timeout)
        return self.lines[0] if self.lines else ""

    def texts(self) -> list[str]:
        return [message_text(l) for l in self.lines if '"type": "user"' in l]


class FakeProc:
    """stdout starts once the first message is written, like real claude,
    so a pre-filled ``result`` can't close stdin before the message."""

    def __init__(self, lines: list[str], sink: list | None = None) -> None:
        self.stdin = FakeStdin(sink)
        self._lines = lines

    @property
    def stdout(self):
        def gen():
            self.stdin.wait_first()
            yield from self._lines

        return gen()


def install(monkeypatch, router, lines=None, *, calls: list | None = None, sink: list | None = None, on_spawn=None):
    """Patch the router's ``streaming_subprocess_open`` with a fake that
    records each spawn (args, cwd, proc) in ``calls``, puts each turn's
    first message text in ``sink``, and calls ``on_spawn(args, cwd)``."""
    lines = lines if lines is not None else ['{"type": "result"}\n']

    @contextlib.contextmanager
    def fake_open(args, cwd, job_id):
        proc = FakeProc(list(lines), sink)
        if calls is not None:
            calls.append({"args": args, "cwd": cwd, "proc": proc})
        if on_spawn is not None:
            on_spawn(args, cwd)
        yield proc

    monkeypatch.setattr(router.claude_runner, "streaming_subprocess_open", fake_open)
    return fake_open


class ControlledProc:
    """A turn that keeps running until the test calls ``end()``; ``emit``
    sends a stdout line (an echo, a delta, a result) while it runs."""

    def __init__(self) -> None:
        import queue as _q

        self.stdin = FakeStdin()
        self._lines: "_q.Queue[str | None]" = _q.Queue()

    def emit(self, line: str) -> None:
        self._lines.put(line if line.endswith("\n") else line + "\n")

    def end(self) -> None:
        self._lines.put(None)

    @property
    def stdout(self):
        def gen():
            self.stdin.wait_first()
            while True:
                line = self._lines.get(timeout=10)
                if line is None:
                    return
                yield line

        return gen()


def install_controlled(monkeypatch, router, procs: list):
    """Every spawn gets a ControlledProc, appended to ``procs``."""

    @contextlib.contextmanager
    def fake_open(args, cwd, job_id):
        proc = ControlledProc()
        procs.append(proc)
        yield proc

    monkeypatch.setattr(router.claude_runner, "streaming_subprocess_open", fake_open)
