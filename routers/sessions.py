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

import json
import threading
from typing import Any, Iterator

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import (
    ANOTHER_CODER_RATE_LIMIT_SESSIONS,
    ANOTHER_CODER_RESUME_MODEL,
    ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
)
from jobs import job_manager
from services import claude_runner
from services.rate_limiter import limiter
from services.session_index import _assistant_blocks, session_index


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _text_delta(event: dict) -> str | None:
    """Token-level text out of a stream-json partial-message event
    (`stream_event` → `content_block_delta` → `text_delta`)."""
    if event.get("type") != "stream_event":
        return None
    inner = event.get("event") or {}
    if inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta") or {}
    return delta.get("text") if delta.get("type") == "text_delta" else None

log = structlog.get_logger()

router = APIRouter()


class ResumeRequest(BaseModel):
    """Body for continuing a session from mobile: one user message that
    gets appended to the transcript via ``claude --resume <id>``."""

    message: str = Field(min_length=1, max_length=100_000)


# One lock per session id: a resume APPENDS to the transcript, so two
# concurrent resumes of the same session would interleave writes. We
# reject the second with 409 rather than queue it. The terminal running
# the same session is a separate process we can't lock from here — that
# stays the user's responsibility (documented: one owner at a time).
_resume_locks: dict[str, threading.Lock] = {}
_resume_locks_guard = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _resume_locks_guard:
        lock = _resume_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _resume_locks[session_id] = lock
        return lock


def _extract_reply(stdout: str) -> str:
    """Pull the assistant's final text out of ``--output-format json``
    (a single result object). Returns '' if the shape is unexpected —
    the client re-fetches the transcript for the canonical turns anyway."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        return data.get("result") or ""
    return ""


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


@router.post("/sessions/{session_id}/resume")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def resume_session(request: Request, session_id: str, body: ResumeRequest) -> dict[str, Any]:
    """Continue a session from mobile. Spawns ``claude --resume <id> -p
    <message>`` in the session's ORIGINAL cwd, which appends the new user
    turn + assistant reply (+ any tool steps) to the same transcript. The
    Mac side is never synced — the ``.jsonl`` file is the single source of
    truth, so the client just re-fetches messages afterward.

    Blocking: waits for the turn to finish (bounded by RESUME_TIMEOUT),
    then returns the reply text. 404 unknown id · 409 already running ·
    502/504 claude failed/timed out."""
    card = session_index.get_card(session_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    cwd = card.get("cwd")
    if not cwd:
        raise HTTPException(status_code=422, detail="session has no recorded cwd; cannot resume")

    # NOTE: intentionally NOT blocking sessions that are live in a terminal.
    # Resuming spawns a second `claude` that appends to the transcript; the
    # open terminal tab keeps stale in-memory context and won't see these
    # turns until it re-reads the file (a fresh `claude -r <id>` or a manual
    # in-tab sync). That's an accepted tradeoff for the mobile-continue flow.

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message must not be empty")

    lock = _lock_for(session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="this session is already processing a message")
    try:
        model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
        args = claude_runner.build_claude_args(
            prompt=message,
            model=model,
            output_format="json",
            extra_flags=["--resume", session_id],
        )
        job = job_manager.create(kind="resume_session")
        log.info("resume_session.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
        result = claude_runner.run_blocking(
            args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS, job_id=job.job_id
        )

        if result.spawn_error:
            raise HTTPException(status_code=500, detail=f"could not spawn claude: {result.spawn_error}")
        if result.timed_out:
            raise HTTPException(
                status_code=504,
                detail=f"claude timed out after {ANOTHER_CODER_RESUME_TIMEOUT_SECONDS}s "
                "(the turn may be partially written; refresh to see it)",
            )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise HTTPException(status_code=502, detail=f"claude exited {result.returncode}: {detail}")

        return {"ok": True, "session_id": session_id, "reply": _extract_reply(result.stdout)}
    finally:
        lock.release()


@router.post("/sessions/{session_id}/resume/stream")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def resume_session_stream(request: Request, session_id: str, body: ResumeRequest):
    """Streaming variant of resume: same `claude --resume` append, but
    emits Server-Sent Events as the reply is generated so the phone can
    render tokens live. Event stream:

        event: text  data: {"chunk": "..."}       # token-level text
        event: tool  data: {"label": "...", "stat": "..."}  # a tool step
        event: done  data: {"session_id": "..."}
        event: error data: {"message": "..."}

    404 unknown id (raised before streaming). Concurrency is guarded by
    the same per-session lock as the blocking endpoint — a second send
    gets an `error` event rather than corrupting the transcript."""
    card = session_index.get_card(session_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    cwd = card.get("cwd")
    if not cwd:
        raise HTTPException(status_code=422, detail="session has no recorded cwd; cannot resume")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message must not be empty")

    def gen() -> Iterator[str]:
        lock = _lock_for(session_id)
        if not lock.acquire(blocking=False):
            yield _sse("error", {"message": "this session is already processing a message"})
            return
        try:
            model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
            args = claude_runner.build_claude_args(
                prompt=message,
                model=model,
                output_format="stream-json",
                extra_flags=["--verbose", "--include-partial-messages", "--resume", session_id],
            )
            job = job_manager.create(kind="resume_session_stream")
            log.info("resume_stream.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
            try:
                with claude_runner.streaming_subprocess(args, cwd=cwd, job_id=job.job_id) as proc:
                    for raw in proc.stdout:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = _text_delta(event)
                        if chunk:
                            yield _sse("text", {"chunk": chunk})
                            continue
                        # Full assistant message → surface its tool steps
                        # (text already streamed via deltas above).
                        if event.get("type") == "assistant":
                            _, tools = _assistant_blocks(event.get("message", {}).get("content"))
                            for t in tools:
                                yield _sse("tool", {"name": t.name, "label": t.label, "stat": t.stat})
            except FileNotFoundError as err:
                yield _sse("error", {"message": f"could not spawn claude: {err}"})
                return
            yield _sse("done", {"session_id": session_id})
        finally:
            lock.release()

    return StreamingResponse(gen(), media_type="text/event-stream")


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
