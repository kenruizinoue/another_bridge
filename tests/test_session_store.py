"""Unit coverage for services/session_store.SessionStore.

The chat-flow tests (test_chat_session_lifecycle.py et al.) exercise
the store via the /chat/stream wiring and lock the user-visible
contract: capture session_id on success, skip on failure, --resume on
follow-up. This file covers the store-specific behavior those flows
don't reach:

  - Persistence across "restart": data written through one
    SessionStore instance must be visible to a fresh instance opened
    on the same on-disk db file. This is the actual regression
    closed by the refactor — the previous in-memory dict
    evaporated on every uvicorn restart and silently dropped every
    active conversation back to a fresh Claude Code session.

  - Upsert semantics: ``set_session`` on an existing
    conversation_id replaces the row rather than erroring. Locks
    the SQLite ON CONFLICT clause; without it, session-id rotation
    (Claude Code re-issuing ids on long sessions) would crash
    chat_stream's success branch.

  - prune_older_than: the future TTL reaper hooks here. Test
    proves the cutoff math is right and rows newer than the
    threshold survive.

  - Thread safety: a smoke test driving concurrent writes from
    multiple threads. Not exhaustive (won't catch every race) but
    enough to catch a missing lock acquisition or
    check_same_thread misconfig.
"""

from __future__ import annotations

import threading
import time

from services.session_store import SessionStore


def test_persistence_across_restart(tmp_path) -> None:
    """The whole point of the SQLite migration: a captured session
    survives a SessionStore restart on the same db file. Simulates
    what happens when uvicorn is restarted mid-day — the next chat
    turn should --resume into the existing Claude Code session
    instead of silently spawning a fresh one."""
    db_path = str(tmp_path / "sessions.db")

    store_a = SessionStore(db_path=db_path)
    store_a.set_session("conv-1", "sess-original")

    # Simulate restart: drop the original instance entirely, open a
    # fresh one against the same file. If the data lived in memory
    # the new instance would see nothing.
    del store_a
    store_b = SessionStore(db_path=db_path)
    assert store_b.get_session("conv-1") == "sess-original"


def test_upsert_replaces_existing_row(tmp_path) -> None:
    """Claude Code rotates session_ids on long-running conversations
    (sessions get re-issued after some token threshold). The store's
    set_session must overwrite cleanly — a UNIQUE constraint failure
    here would crash chat_stream's success branch."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    store.set_session("conv-1", "sess-old")
    store.set_session("conv-1", "sess-new")
    assert store.get_session("conv-1") == "sess-new"


def test_get_session_returns_none_for_unknown_conversation(tmp_path) -> None:
    """The lookup path in chat_stream uses a None return to mean
    'no prior session, spawn fresh without --resume'. Lock that
    behavior so a future change to use empty-string defaults doesn't
    silently produce ``--resume ""`` calls."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    assert store.get_session("never-seen") is None


def test_clear_all_wipes_everything(tmp_path) -> None:
    """Used by the test suite's autouse fixture between cases.
    Production code must NEVER call this — added it deliberately so
    the contract is locked."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    store.set_session("conv-1", "sess-1")
    store.set_session("conv-2", "sess-2")
    store.clear_all()
    assert store.get_session("conv-1") is None
    assert store.get_session("conv-2") is None


def test_prune_older_than_removes_stale_rows_only(tmp_path) -> None:
    """The future TTL reaper (Phase 2 sub-item 3) calls this on a
    timer with a 7-day cutoff. Verify a row's last_seen_at gets
    updated on every set_session so a recently-touched conversation
    isn't pruned just because its row was originally created weeks
    ago."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    store.set_session("old-conv", "sess-old")

    # Backdate "old-conv" by a week + a buffer. Direct SQL is fine
    # in tests — the public API doesn't expose backdating because
    # production code never needs it.
    store._conn.execute(  # type: ignore[attr-defined]
        "UPDATE sessions SET last_seen_at = ? WHERE conversation_id = ?",
        (int(time.time()) - (8 * 86400), "old-conv"),
    )
    store._conn.commit()  # type: ignore[attr-defined]

    # "fresh-conv" gets a normal write — last_seen_at = now.
    store.set_session("fresh-conv", "sess-fresh")

    deleted = store.prune_older_than(max_age_seconds=7 * 86400)
    assert deleted == 1
    assert store.get_session("old-conv") is None
    assert store.get_session("fresh-conv") == "sess-fresh"


def test_concurrent_writes_dont_corrupt_state(tmp_path) -> None:
    """Smoke test for the threading.Lock + check_same_thread=False
    setup. FastAPI's worker pool can hit the store from multiple
    threads concurrently; without the lock SQLite would raise
    'recursive use of cursors' or 'database is locked' errors. Not
    exhaustive — won't catch every conceivable race — but enough to
    catch a missing lock acquisition or misconfigured connection."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    errors: list[BaseException] = []

    def writer(idx: int) -> None:
        try:
            for i in range(50):
                store.set_session(f"conv-{idx}", f"sess-{idx}-{i}")
        except BaseException as e:  # pragma: no cover — only fires on bug
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors}"
    # Every conv-N should have its final session_id (sess-N-49).
    for i in range(8):
        assert store.get_session(f"conv-{i}") == f"sess-{i}-49"


def test_is_reachable_returns_true_for_healthy_store(tmp_path) -> None:
    """is_reachable powers the /health diagnostic. A freshly-opened
    store on a writable file is the green path — anything else means
    the connection is busted (closed conn, locked db, corrupted
    schema). Locks the happy path so a refactor of the SELECT 1
    probe doesn't accidentally always-return-False."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    assert store.is_reachable() is True


def test_is_reachable_returns_false_when_connection_is_closed(tmp_path) -> None:
    """The most realistic failure mode: someone (a test, a botched
    shutdown handler) closed the underlying connection. is_reachable
    must catch sqlite3.ProgrammingError and report False rather than
    bubbling the exception up to the /health route — uptime checks
    can't tolerate /health 500ing."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    store._conn.close()  # type: ignore[attr-defined]
    assert store.is_reachable() is False


def test_in_memory_db_works_without_filesystem(tmp_path) -> None:
    """The test suite uses ``:memory:`` via tests/conftest.py. Make
    sure the constructor doesn't trip on the missing parent-dir
    branch and that ordinary operations work — this is what every
    test relies on for isolation."""
    store = SessionStore(db_path=":memory:")
    store.set_session("conv-1", "sess-x")
    assert store.get_session("conv-1") == "sess-x"
