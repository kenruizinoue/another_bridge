"""Background pruner for the in-memory ``JobManager._jobs`` dict and
the SQLite-backed session store.

Without this, a long-running ``uvicorn`` process accumulates state
indefinitely:

  * ``jobs.py``: every webhook call (``instruct_planning`` /
    ``instruct_implementation``) and every chat turn creates a Job
    entry that lives forever after the subprocess exits — the dict
    grows by ~1 entry per active conversation per turn and never
    shrinks. A coder running for weeks accumulates thousands of dead
    ``done``/``failed`` Jobs.

  * ``session_store``: each row stays until something explicitly
    deletes it. Conversations the platform stops serving (account
    deleted, conversation cleaned up) leave orphan rows with their
    Claude Code session id — harmless individually but unbounded
    over the life of the deployment.

The reaper runs in a daemon thread, calls
``JobManager.prune_finished_older_than`` and
``SessionStore.prune_older_than`` on a slow interval, and logs the
counts. Defaults (1h job retention, 7d session retention, 10min
check interval) are sized so the reaper has near-zero impact on
request-path latency and never deletes anything that's still
referenced by an active poll.

Lifecycle is owned by FastAPI's ``lifespan`` context manager in
``main.py`` — ``reaper.start()`` on startup, ``reaper.stop()`` after
the yield. Tests instantiate ``Reaper`` directly without starting
the thread and drive ``prune_once()`` synchronously.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import structlog

from jobs import JobManager
from services.session_store import SessionStore


# Defaults sized for a single-user / small-team deployment. Override
# via env in production if a longer post-mortem window is wanted on
# failed jobs (e.g. for trace forensics).
JOB_TTL_SECONDS_DEFAULT = 60 * 60                # 1h: status endpoints rarely
                                                 # care about a result older
                                                 # than this — the platform's
                                                 # poller has already moved on.
SESSION_TTL_SECONDS_DEFAULT = 7 * 24 * 60 * 60   # 7d: roughly the dwell time
                                                 # of an active conversation
                                                 # before the user moves on.
REAPER_INTERVAL_SECONDS_DEFAULT = 10 * 60        # 10min: low frequency keeps
                                                 # the daemon out of the way of
                                                 # the request path; reaper
                                                 # latency doesn't matter.


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


log = structlog.get_logger()


class Reaper:
    """TTL reaper for ``JobManager._jobs`` and ``SessionStore``.

    Construct once at app startup, hand it the singletons, call
    ``start()``. The daemon thread sleeps in chunks via an Event so
    ``stop()`` can short-circuit the wait — joining on a bare
    ``time.sleep`` would leave shutdown hanging up to the full
    interval.
    """

    def __init__(
        self,
        job_manager: JobManager,
        session_store: SessionStore,
        *,
        job_ttl_seconds: int = JOB_TTL_SECONDS_DEFAULT,
        session_ttl_seconds: int = SESSION_TTL_SECONDS_DEFAULT,
        interval_seconds: int = REAPER_INTERVAL_SECONDS_DEFAULT,
    ) -> None:
        self._job_manager = job_manager
        self._session_store = session_store
        self.job_ttl_seconds = job_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def prune_once(self) -> tuple[int, int]:
        """Run a single prune cycle synchronously. Returns
        ``(jobs_deleted, sessions_deleted)`` for logging + tests.

        Called from the daemon's loop AND from tests that want to
        drive a cycle without actually starting a thread. Errors from
        either prune are logged at warning and swallowed — a transient
        DB lock or a momentary lock contention shouldn't take down the
        entire reaper for the rest of the process lifetime."""
        try:
            jobs_deleted = self._job_manager.prune_finished_older_than(
                self.job_ttl_seconds,
            )
        except Exception as err:  # noqa: BLE001 — we want to keep running
            log.warning("reaper.jobs_prune_failed", err=str(err))
            jobs_deleted = 0
        try:
            sessions_deleted = self._session_store.prune_older_than(
                self.session_ttl_seconds,
            )
        except Exception as err:  # noqa: BLE001
            log.warning("reaper.sessions_prune_failed", err=str(err))
            sessions_deleted = 0
        if jobs_deleted or sessions_deleted:
            log.info(
                "reaper.pruned",
                jobs_deleted=jobs_deleted,
                sessions_deleted=sessions_deleted,
            )
        return jobs_deleted, sessions_deleted

    def start(self) -> None:
        """Spawn the daemon. Idempotent — calling twice is a no-op
        instead of an error so a duplicate ``lifespan`` invocation
        (e.g. a hot-reload edge case) doesn't crash startup."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="another-coder-reaper",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "reaper.started",
            job_ttl_seconds=self.job_ttl_seconds,
            session_ttl_seconds=self.session_ttl_seconds,
            interval_seconds=self.interval_seconds,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the daemon and wait briefly for it to exit. The
        thread is daemonized so a process exit will reap it anyway,
        but joining lets shutdown logs land in order."""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        log.info("reaper.stopped")

    def _run(self) -> None:
        # Run an initial prune as soon as the thread starts so a
        # restart of a long-lived deploy with backlog of stale data
        # doesn't have to wait a full interval before the first
        # cleanup. Subsequent cycles use the configured interval.
        self.prune_once()
        while not self._stop_event.wait(self.interval_seconds):
            self.prune_once()


def build_default_reaper() -> Reaper:
    """Compose a reaper from the module-level singletons + env-tuned
    thresholds. Called by ``main.py``'s lifespan; tests use the class
    directly with explicit arguments."""
    from jobs import job_manager
    from services.session_store import session_store

    return Reaper(
        job_manager=job_manager,
        session_store=session_store,
        job_ttl_seconds=_env_seconds(
            "ANOTHER_CODER_JOB_TTL_SECONDS",
            JOB_TTL_SECONDS_DEFAULT,
        ),
        session_ttl_seconds=_env_seconds(
            "ANOTHER_CODER_SESSION_TTL_SECONDS",
            SESSION_TTL_SECONDS_DEFAULT,
        ),
        interval_seconds=_env_seconds(
            "ANOTHER_CODER_REAPER_INTERVAL_SECONDS",
            REAPER_INTERVAL_SECONDS_DEFAULT,
        ),
    )
