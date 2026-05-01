"""Test-suite configuration loaded by pytest before any test module.

Module-level statements here run during collection, BEFORE pytest
imports the test modules — and crucially before any of those
imports transitively pull in services/session_store.py and create
the on-disk SQLite singleton. By setting
``ANOTHER_CODER_SESSION_DB_PATH=":memory:"`` here we keep the test
suite from ever touching ``~/.another_coder/sessions.db`` (or
whatever path a developer has configured for local dev) and remove
the cross-test pollution risk that comes with a shared on-disk file.

The autouse fixtures below provide per-test cleanup so individual
test files don't have to duplicate it:

  - ``_clean_session_store`` — wipes SessionStore rows between cases.
  - ``_clean_jobs`` — drops jobs the test created from JobManager.

Shared fixtures (opt-in, not autouse):

  - ``tmp_git_repo`` — initializes a real git repo in a tmp_path with
    one commit + a bare-repo origin remote. Used by tests that need
    to exercise git_service helpers / implementation flow without
    inlining their own ``git init`` boilerplate.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Must run before any `from services.session_store import ...` happens.
# pytest evaluates conftest.py before collecting + importing test
# modules, so this assignment lands first.
os.environ.setdefault("ANOTHER_CODER_SESSION_DB_PATH", ":memory:")

import pytest


@pytest.fixture(autouse=True)
def _clean_session_store():
    """Wipe the session_store between every test so an earlier test's
    captured (conversation_id, session_id) pair can't leak into a
    later test's --resume lookup. Replaces the per-test
    ``chat_router._session_map.clear()`` autouse fixtures that lived
    in test_chat_polling.py et al. before the SQLite migration."""
    from services.session_store import session_store

    session_store.clear_all()
    yield
    session_store.clear_all()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Drain slowapi's in-memory bucket between tests so a heavy-traffic
    test in one file (or a new test class that adds more /chat/stream
    calls) can't exhaust the per-route cap and trip 429s in unrelated
    later tests. All TestClient calls share the same fake remote-IP
    (``testclient``), so without a reset the bucket accumulates across
    the whole suite. Production deploys aren't affected — every real
    caller has a distinct IP/key."""
    from services.rate_limiter import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _clean_jobs():
    """Drop any jobs the test created so the module-level JobManager
    singleton doesn't leak between tests. Captures the existing job
    set on entry, then deletes everything new on exit — preserves
    jobs created by other infrastructure (none today; defensive).
    Centralized here so the four chat-related test files that used
    to duplicate this fixture stay focused on their actual contracts."""
    from jobs import job_manager

    before: set[str] = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    yield
    after = set(job_manager._jobs.keys())  # type: ignore[attr-defined]
    for job_id in after - before:
        job_manager._jobs.pop(job_id, None)  # type: ignore[attr-defined]


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Build a real on-disk git repo with one commit + a bare-repo
    origin remote. Returns the working-tree path. Tests that exercise
    git_service helpers (``_run_git``, branch existence, base-branch
    resolution) need a real repo because the helpers shell out to
    actual git commands.

    Layout::

      tmp_path/
        origin.git/   # bare remote (push/fetch target)
        repo/         # working tree, returned to the test
          .git/
          README.md
    """
    work = tmp_path / "repo"
    work.mkdir()
    bare = tmp_path / "origin.git"
    bare.mkdir()

    def _run(args: list[str], cwd: Path) -> None:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {cwd}: {result.stderr}"
            )

    _run(["git", "init", "--bare", "--initial-branch=main"], bare)
    _run(["git", "init", "--initial-branch=main"], work)
    # Pin author/committer so the commit doesn't error in CI envs
    # that don't have a global git identity.
    _run(["git", "config", "user.email", "test@example.com"], work)
    _run(["git", "config", "user.name", "Test User"], work)

    (work / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], work)
    _run(["git", "commit", "-m", "initial"], work)
    _run(["git", "remote", "add", "origin", str(bare)], work)
    _run(["git", "push", "-u", "origin", "main"], work)

    return work
