"""Test-suite configuration loaded by pytest before any test module.

Module-level statements here run during collection, BEFORE pytest
imports the test modules — and crucially before any of those
imports transitively pull in services/session_store.py and create
the on-disk SQLite singleton. By setting
``ANOTHER_CODER_SESSION_DB_PATH=":memory:"`` here we keep the test
suite from ever touching ``~/.another_coder/sessions.db`` (or
whatever path a developer has configured for local dev) and remove
the cross-test pollution risk that comes with a shared on-disk file.

The autouse ``_clean_session_store`` fixture clears rows between
cases so state from one test can't leak into the next.
"""

from __future__ import annotations

import os

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
