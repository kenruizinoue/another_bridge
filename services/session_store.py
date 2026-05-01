"""Persistent conversation_id → Claude Code session_id store.

Replaces the in-memory ``_session_map`` that lived at the top of
``routers/chat.py``. The dict was lost on every ``uvicorn`` restart,
so a mid-day deploy or a crash silently dropped every active
conversation back to a fresh Claude Code session — losing transcript
context the user expected to be carried forward.

SQLite is overkill for the data shape (one row per conversation, two
short string fields, occasional last-seen update) but it's already in
the standard library, gives us crash-safe persistence essentially for
free, and unblocks the future TTL reaper item — the daemon thread
just runs ``DELETE FROM sessions WHERE last_seen_at < ?``.

Threading: FastAPI runs sync handlers in a worker pool, so multiple
threads can hit the store concurrently. SQLite's connection isn't
thread-safe by default (`check_same_thread=True`) — we relax that
flag and serialize every operation behind a single ``threading.Lock``.
A real lock dance (per-thread connections, WAL-only readers) would
let parallel reads through, but a session lookup is microseconds and
the contention is purely theoretical at our request rate. WAL mode
is still on for crash safety, not concurrency.

Schema: ``sessions(conversation_id PRIMARY KEY, session_id NOT NULL,
last_seen_at INTEGER)``. ``last_seen_at`` is a Unix epoch second
(small, monotonic, easy to compare) and gets updated on every
``set_session`` call so the future reaper can prune stale rows
without a separate write path.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


# Path resolution lives in config.py — pydantic-settings reads
# ANOTHER_CODER_SESSION_DB_PATH at import and falls back to
# ``~/.another_coder/sessions.db`` when unset. Tests set the env
# var to ``":memory:"`` via ``tests/conftest.py`` BEFORE config is
# imported, so no real on-disk file gets created during the suite.
from config import ANOTHER_CODER_SESSION_DB_PATH as DEFAULT_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    conversation_id TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    last_seen_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_last_seen_at_idx
    ON sessions(last_seen_at);
"""


class SessionStore:
    """Thread-safe SQLite-backed conversation_id → session_id store.

    All operations acquire ``self._lock`` so the underlying connection
    sees serialized access. Connection is opened with
    ``check_same_thread=False`` because uvicorn worker threads vary
    request to request.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        # Ensure the parent dir exists for on-disk paths. ":memory:" /
        # "file::memory:?..." style URIs have no parent and pathlib
        # handles them as a relative path — only mkdir when there's a
        # real directory above the file.
        if db_path != ":memory:":
            parent = Path(db_path).parent
            if str(parent) and parent != Path("."):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL gives us crash-safe persistence without forcing fsync on
        # every write. ":memory:" databases don't support journal_mode
        # = wal; SQLite silently keeps them in the default mode and
        # PRAGMA returns "memory" — the call is harmless either way.
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_session(self, conversation_id: str) -> Optional[str]:
        """Look up the captured Claude Code session_id for a
        conversation. Returns None when the conversation is fresh or
        the prior run failed before persistence (see set_session
        contract — failed runs deliberately leave the row absent so
        the next attempt starts from scratch)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT session_id FROM sessions WHERE conversation_id = ?",
                (conversation_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_session(self, conversation_id: str, session_id: str) -> None:
        """Upsert the (conversation_id, session_id) pair and stamp
        last_seen_at = now. Called from chat_stream's success path
        AFTER a clean Claude Code run — failed/cancelled runs skip
        this so a retry can start fresh instead of trying to --resume
        into a broken session."""
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (conversation_id, session_id, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (conversation_id, session_id, now),
            )
            self._conn.commit()

    def clear_all(self) -> None:
        """Wipe every row. Used by the test suite's autouse fixtures
        between cases to keep state isolated. Not exposed on any HTTP
        route — production state should never be cleared wholesale."""
        with self._lock:
            self._conn.execute("DELETE FROM sessions")
            self._conn.commit()

    def is_reachable(self) -> bool:
        """Return True iff the SQLite connection responds to a trivial
        query. Surfaced on /health so an operator on a remote deploy
        can tell ``process up but DB locked`` apart from a healthy
        bridge without tailing logs. Cheap — ``SELECT 1`` doesn't
        scan rows or hit disk."""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def prune_older_than(self, max_age_seconds: int) -> int:
        """Delete rows whose ``last_seen_at`` is older than the
        threshold. Returns the deleted-row count for logging. Called
        by the TTL reaper daemon on a timer; standalone helper here
        so the reaper module stays thin."""
        cutoff = int(time.time()) - max_age_seconds
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE last_seen_at < ?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount


# Module-level singleton — every chat_stream request shares the same
# connection. Mirrors the existing job_manager pattern; both could
# eventually move behind FastAPI Depends() but that's a separate
# refactor.
session_store = SessionStore()
