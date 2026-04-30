"""Coverage for the TTL reaper and ``JobManager.prune_finished_older_than``.

The reaper exists to keep a long-running ``uvicorn`` process from
accumulating stale state forever — without it, ``JobManager._jobs``
grows by ~1 entry per webhook call / chat turn and never shrinks,
and ``session_store`` keeps rows for conversations the platform has
long since stopped serving. The contract these tests lock:

  * ``JobManager.prune_finished_older_than`` only deletes
    ``done`` / ``failed`` entries with a ``finished_at`` past the
    cutoff. ``running`` jobs are NEVER touched — even ones with
    pathologically long uptimes — because the cancel/status routes
    depend on those entries existing.

  * ``Reaper.prune_once`` drives both prunes synchronously (used by
    the daemon loop AND directly by tests so we don't need to spin
    up a real thread). Errors from either prune are swallowed +
    logged — a transient DB lock or job-dict contention must not
    take down the reaper for the rest of the process lifetime.

  * The thread lifecycle (``start`` / ``stop``) is idempotent and
    cleanly joinable. We don't drive a real long-running cycle
    here — that would be flaky — but we DO verify start spawns a
    thread, an immediate stop cleanly joins it, and start-while-
    already-running is a no-op.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from jobs import Job, JobManager
from services.reaper import Reaper, build_default_reaper
from services.session_store import SessionStore


# ──────────────────────────────────────────────────────────────────────
# JobManager.prune_finished_older_than — unit
# ──────────────────────────────────────────────────────────────────────


class TestPruneFinishedOlderThan:
    def test_deletes_finished_jobs_past_cutoff(self) -> None:
        mgr = JobManager()
        old = mgr.create("instruct_planning")
        mgr.mark_done(old.job_id, {"plan": "old"})
        # Backdate finished_at to 2h ago — past the 1h default cutoff
        # the reaper applies in production. Direct attribute mutation
        # is fine in tests; the public API doesn't expose backdating.
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[old.job_id].finished_at = time.time() - 7200  # type: ignore[attr-defined]

        deleted = mgr.prune_finished_older_than(3600)
        assert deleted == 1
        assert mgr.get(old.job_id) is None

    def test_preserves_recent_finished_jobs(self) -> None:
        mgr = JobManager()
        recent = mgr.create("instruct_planning")
        mgr.mark_done(recent.job_id, {"plan": "recent"})
        # finished_at = now (default after mark_done) — well within the
        # 1h cutoff, must survive.

        deleted = mgr.prune_finished_older_than(3600)
        assert deleted == 0
        assert mgr.get(recent.job_id) is not None

    def test_never_prunes_running_jobs_even_when_old(self) -> None:
        # The cancel + status routes depend on running jobs existing
        # for as long as the subprocess is alive. A pathologically
        # long-running planning run (say a 24h Claude Code session)
        # MUST stay in the dict — pruning it would 404 the cancel
        # button and silently lose the result on completion.
        mgr = JobManager()
        running = mgr.create("instruct_planning")
        # Pretend it started 25h ago; status is still "running" so the
        # finished_at predicate is None and the row should be skipped.
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[running.job_id].started_at = time.time() - 25 * 3600  # type: ignore[attr-defined]

        deleted = mgr.prune_finished_older_than(3600)
        assert deleted == 0
        assert mgr.get(running.job_id) is not None

    def test_mixed_jobs_only_old_finished_removed(self) -> None:
        mgr = JobManager()
        old_done = mgr.create("instruct_planning")
        mgr.mark_done(old_done.job_id, {"plan": "old"})
        old_failed = mgr.create("chat_stream")
        mgr.mark_failed(old_failed.job_id, "spawn failed")
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[old_done.job_id].finished_at = time.time() - 7200  # type: ignore[attr-defined]
            mgr._jobs[old_failed.job_id].finished_at = time.time() - 7200  # type: ignore[attr-defined]
        recent_done = mgr.create("instruct_implementation")
        mgr.mark_done(recent_done.job_id, {"branch": "agent/ticket-1"})
        running = mgr.create("instruct_planning")

        deleted = mgr.prune_finished_older_than(3600)
        assert deleted == 2
        assert mgr.get(old_done.job_id) is None
        assert mgr.get(old_failed.job_id) is None
        assert mgr.get(recent_done.job_id) is not None
        assert mgr.get(running.job_id) is not None

    def test_returns_zero_when_nothing_to_prune(self) -> None:
        mgr = JobManager()
        deleted = mgr.prune_finished_older_than(3600)
        assert deleted == 0


# ──────────────────────────────────────────────────────────────────────
# Reaper.prune_once — drives both prunes
# ──────────────────────────────────────────────────────────────────────


class TestReaperPruneOnce:
    def _build(
        self,
        tmp_path,
        *,
        job_ttl: int = 3600,
        session_ttl: int = 7 * 86400,
    ) -> tuple[Reaper, JobManager, SessionStore]:
        mgr = JobManager()
        store = SessionStore(db_path=str(tmp_path / "sessions.db"))
        reaper = Reaper(
            job_manager=mgr,
            session_store=store,
            job_ttl_seconds=job_ttl,
            session_ttl_seconds=session_ttl,
        )
        return reaper, mgr, store

    def test_drives_both_prunes_and_returns_counts(self, tmp_path) -> None:
        reaper, mgr, store = self._build(tmp_path)

        # Stale job — finished 2h ago, will be pruned at 1h cutoff.
        old = mgr.create("instruct_planning")
        mgr.mark_done(old.job_id, {"plan": "x"})
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[old.job_id].finished_at = time.time() - 7200  # type: ignore[attr-defined]

        # Stale session — last_seen 8d ago, will be pruned at 7d cutoff.
        store.set_session("conv-old", "sess-old")
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE sessions SET last_seen_at = ? WHERE conversation_id = ?",
            (int(time.time()) - 8 * 86400, "conv-old"),
        )
        store._conn.commit()  # type: ignore[attr-defined]

        # Fresh entries — must survive.
        fresh = mgr.create("instruct_planning")
        mgr.mark_done(fresh.job_id, {"plan": "y"})
        store.set_session("conv-fresh", "sess-fresh")

        jobs_deleted, sessions_deleted = reaper.prune_once()
        assert jobs_deleted == 1
        assert sessions_deleted == 1
        assert mgr.get(fresh.job_id) is not None
        assert store.get_session("conv-fresh") == "sess-fresh"

    def test_swallows_job_prune_errors(self, tmp_path) -> None:
        # A broken JobManager mock must not crash the reaper — we want
        # the session-store prune to still run and the daemon loop to
        # keep going on subsequent ticks.
        reaper, _, store = self._build(tmp_path)
        broken_mgr = MagicMock()
        broken_mgr.prune_finished_older_than.side_effect = RuntimeError("boom")
        reaper._job_manager = broken_mgr  # type: ignore[attr-defined]

        # Pre-seed a stale session so we can verify the second prune
        # still ran despite the first one blowing up.
        store.set_session("conv-old", "sess-old")
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE sessions SET last_seen_at = ? WHERE conversation_id = ?",
            (int(time.time()) - 30 * 86400, "conv-old"),
        )
        store._conn.commit()  # type: ignore[attr-defined]

        jobs_deleted, sessions_deleted = reaper.prune_once()
        assert jobs_deleted == 0
        assert sessions_deleted == 1

    def test_swallows_session_prune_errors(self, tmp_path) -> None:
        # Symmetric: a broken session store must not stop job prunes.
        reaper, mgr, _ = self._build(tmp_path)
        broken_store = MagicMock()
        broken_store.prune_older_than.side_effect = RuntimeError("db locked")
        reaper._session_store = broken_store  # type: ignore[attr-defined]

        old = mgr.create("instruct_planning")
        mgr.mark_done(old.job_id, {"plan": "x"})
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[old.job_id].finished_at = time.time() - 7200  # type: ignore[attr-defined]

        jobs_deleted, sessions_deleted = reaper.prune_once()
        assert jobs_deleted == 1
        assert sessions_deleted == 0


# ──────────────────────────────────────────────────────────────────────
# Reaper.start / stop — thread lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestReaperLifecycle:
    def test_start_spawns_thread_and_runs_initial_prune(self, tmp_path) -> None:
        # The first prune fires immediately — long deploys with backlog
        # shouldn't have to wait a full interval before any cleanup.
        # Verify by stubbing prune_once and checking it was called at
        # least once before stop returns.
        mgr = JobManager()
        store = SessionStore(db_path=str(tmp_path / "sessions.db"))
        reaper = Reaper(
            job_manager=mgr,
            session_store=store,
            interval_seconds=3600,  # large so the second tick won't fire
        )
        call_count = 0
        cycled = threading.Event()

        original = reaper.prune_once

        def counting_prune_once():
            nonlocal call_count
            call_count += 1
            cycled.set()
            return original()

        reaper.prune_once = counting_prune_once  # type: ignore[method-assign]

        reaper.start()
        assert cycled.wait(timeout=2.0), "initial prune did not fire"
        reaper.stop()
        assert call_count >= 1

    def test_start_is_idempotent(self, tmp_path) -> None:
        # A duplicate lifespan invocation (hot-reload edge case) must
        # not crash startup or spawn two daemons.
        mgr = JobManager()
        store = SessionStore(db_path=str(tmp_path / "sessions.db"))
        reaper = Reaper(
            job_manager=mgr,
            session_store=store,
            interval_seconds=3600,
        )
        try:
            reaper.start()
            first_thread = reaper._thread  # type: ignore[attr-defined]
            reaper.start()
            second_thread = reaper._thread  # type: ignore[attr-defined]
            assert first_thread is second_thread
        finally:
            reaper.stop()

    def test_stop_cleanly_joins(self, tmp_path) -> None:
        # The Event-based wait means stop() doesn't have to sit through
        # the full interval — short-circuits via stop_event.set().
        # Verify by timing stop() against a long interval.
        mgr = JobManager()
        store = SessionStore(db_path=str(tmp_path / "sessions.db"))
        reaper = Reaper(
            job_manager=mgr,
            session_store=store,
            interval_seconds=3600,
        )
        reaper.start()
        # Give the thread a moment to enter the wait().
        time.sleep(0.05)
        t0 = time.monotonic()
        reaper.stop(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, (
            f"stop took {elapsed}s — should be near-instant via the Event"
        )

    def test_stop_without_start_is_safe(self, tmp_path) -> None:
        mgr = JobManager()
        store = SessionStore(db_path=str(tmp_path / "sessions.db"))
        reaper = Reaper(job_manager=mgr, session_store=store)
        # Should not raise even though start() never ran.
        reaper.stop()


# ──────────────────────────────────────────────────────────────────────
# build_default_reaper — env-tuned construction
# ──────────────────────────────────────────────────────────────────────


class TestBuildDefaultReaper:
    def test_default_thresholds_when_no_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ANOTHER_CODER_JOB_TTL_SECONDS", raising=False)
        monkeypatch.delenv("ANOTHER_CODER_SESSION_TTL_SECONDS", raising=False)
        monkeypatch.delenv("ANOTHER_CODER_REAPER_INTERVAL_SECONDS", raising=False)

        reaper = build_default_reaper()
        assert reaper.job_ttl_seconds == 3600
        assert reaper.session_ttl_seconds == 7 * 86400
        assert reaper.interval_seconds == 600

    def test_env_overrides_thresholds(self, monkeypatch) -> None:
        monkeypatch.setenv("ANOTHER_CODER_JOB_TTL_SECONDS", "120")
        monkeypatch.setenv("ANOTHER_CODER_SESSION_TTL_SECONDS", "3600")
        monkeypatch.setenv("ANOTHER_CODER_REAPER_INTERVAL_SECONDS", "30")

        reaper = build_default_reaper()
        assert reaper.job_ttl_seconds == 120
        assert reaper.session_ttl_seconds == 3600
        assert reaper.interval_seconds == 30

    def test_invalid_env_falls_back_to_defaults(self, monkeypatch) -> None:
        # Mistyped env vars (e.g. "1h" instead of "3600") shouldn't
        # crash startup — fall back to the defaults instead. This
        # matters because the env override is a foot-gun otherwise:
        # a typo in production would either crash the boot or, worse,
        # silently disable the reaper.
        monkeypatch.setenv("ANOTHER_CODER_JOB_TTL_SECONDS", "not-a-number")
        monkeypatch.setenv("ANOTHER_CODER_SESSION_TTL_SECONDS", "0")
        monkeypatch.setenv("ANOTHER_CODER_REAPER_INTERVAL_SECONDS", "-5")

        reaper = build_default_reaper()
        assert reaper.job_ttl_seconds == 3600
        assert reaper.session_ttl_seconds == 7 * 86400
        assert reaper.interval_seconds == 600
