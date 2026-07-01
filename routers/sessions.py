"""Read-only session browsing — the data behind the mobile card list.

``GET /sessions`` enumerates every Claude Code conversation on disk
(including terminal-born ones the bridge never created) as card
metadata; ``GET /sessions/{id}`` returns one card. Both are pure reads
served from ``services/session_index`` — nothing here resumes or
mutates a session. Wiring a tapped card to ``claude --resume <id>`` is a
separate, state-changing endpoint that lives with the runner.

Auth + rate limiting are applied the same way as the rest of the
bridge: the include-level ``verify_api_key`` gate in main.py, plus a
per-route slowapi limit keyed on the shared X-Coder-Key.
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from config import ANOTHER_CODER_RATE_LIMIT_SESSIONS
from services.rate_limiter import limiter
from services.session_index import session_index

log = structlog.get_logger()

router = APIRouter()


@router.get("/sessions")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def list_sessions(
    request: Request,
    q: str | None = Query(default=None, description="case-insensitive substring over title / project / cwd"),
    project: str | None = Query(default=None, description="exact project label filter"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Paginated, newest-activity-first list of conversation cards.

    Response shape is picker-ready:
        {"sessions": [<card>, ...], "total": M, "limit": L, "offset": O}
    where ``total`` is the filtered count before pagination so the app
    can show "N of M" and drive infinite scroll."""
    sessions, total = session_index.list_cards(
        query=q, project=project, limit=limit, offset=offset
    )
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


@router.get("/sessions/{session_id}/messages")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def get_session_messages(
    request: Request,
    session_id: str,
    before: int | None = Query(
        default=None, ge=0,
        description="exclusive upper-bound turn index; omit for the latest page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """One NEWEST-FIRST page of a conversation's renderable turns, for an
    inverted chat list. The transcript is filtered server-side (tool
    output, thinking, sidechains stripped) so the phone never receives
    the raw multi-MB event log. 404 when the session id matches no file.

    Paging older: start with no ``before`` (latest page), then pass the
    returned ``next_before`` to walk toward the first message."""
    page = session_index.get_messages(session_id, before=before, limit=limit)
    if page is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return page


@router.get("/sessions/{session_id}")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    """Single card by session id. 404 when no transcript matches — the
    id may be stale (session reaped by Claude Code's own 30-day cleanup)
    or simply wrong."""
    card = session_index.get_card(session_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return card
