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

import base64
import binascii
import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import (
    ANOTHER_CODER_ATTACHMENTS_DIR,
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


class ImageAttachment(BaseModel):
    """A base64 image the phone attached to a message (raw base64, no
    ``data:`` prefix)."""

    media_type: str = Field(pattern=r"^image/(png|jpeg|jpg|webp|gif)$")
    data: str = Field(min_length=1)


# Extensions the file-drop path accepts: things claude can Read (PDF via
# the Read tool's native support; everything else is text). Media that
# claude cannot consume (video/audio/binaries) is rejected up front.
ALLOWED_FILE_EXTENSIONS = frozenset(
    {
        "pdf", "txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml",
        "log", "html", "css", "js", "jsx", "ts", "tsx", "py", "java",
        "kt", "swift", "c", "h", "cpp", "hpp", "rb", "go", "rs", "sh",
        "sql", "toml", "ini", "cfg", "conf",
    }
)
MAX_FILES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024  # decoded size, per file


class FileAttachment(BaseModel):
    """A base64 file the phone attached (raw base64, no ``data:`` prefix).

    Unlike images (inlined as content blocks so the model SEES them),
    files are saved to disk on the Mac and the message references their
    paths — the resumed claude Reads them itself. One mechanism for
    every allowed extension, and a 40MB CSV doesn't blow up the prompt."""

    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    """Body for continuing a session from mobile: a user message and/or
    attachments (up to 10 inline images, up to 5 dropped files), appended
    to the transcript via ``claude --resume <id>``. At least one of
    message / images / files must be present."""

    message: str = Field(default="", max_length=100_000)
    images: list[ImageAttachment] = Field(default_factory=list, max_length=10)
    files: list[FileAttachment] = Field(default_factory=list, max_length=MAX_FILES)


def _safe_filename(name: str) -> str:
    """Bare, traversal-proof filename: basename only, conservative
    charset, bounded length, extension checked against the allowlist."""
    base = Path(name).name  # strips any path components
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "file"
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext not in ALLOWED_FILE_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported file type: .{ext or '?'} "
            f"(allowed: {', '.join(sorted(ALLOWED_FILE_EXTENSIONS))})",
        )
    return base[-80:]


def _save_files(session_id: str, files: list[FileAttachment]) -> list[str]:
    """Decode + write attachments under ATTACHMENTS_DIR/<session_id>/ and
    return the absolute paths. 422 on bad base64, oversize, or disallowed
    extension — all raised BEFORE any resume starts, so a bad attachment
    never half-runs a turn."""
    if not files:
        return []
    # session_id comes from the URL path; keep the subdir name boring.
    session_dir = ANOTHER_CODER_ATTACHMENTS_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for f in files:
        try:
            blob = base64.b64decode(f.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail=f"file {f.name!r}: invalid base64")
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=422,
                detail=f"file {f.name!r} is {len(blob) // (1024 * 1024)}MB; max {MAX_FILE_BYTES // (1024 * 1024)}MB",
            )
        target = session_dir / f"{uuid.uuid4().hex[:8]}-{_safe_filename(f.name)}"
        target.write_bytes(blob)
        paths.append(str(target))
    log.info("resume.files_saved", session_id=session_id, count=len(paths))
    return paths


def _with_files_footer(message: str, file_paths: list[str]) -> str:
    """The message claude actually receives: the user's text plus a footer
    pointing at the saved attachments for it to Read."""
    if not file_paths:
        return message
    listing = "\n".join(f"- {p}" for p in file_paths)
    footer = f"Attached files saved on this Mac (open them with the Read tool as needed):\n{listing}"
    return f"{message}\n\n{footer}" if message else footer


def _stdin_message(message: str, images: list[ImageAttachment]) -> str:
    """A stream-json user message (text + image content blocks) for
    ``--input-format stream-json`` on claude's stdin."""
    content: list[dict] = []
    if message:
        content.append({"type": "text", "text": message})
    for img in images:
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": img.media_type, "data": img.data}}
        )
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


# In-flight resume tracking, keyed by session id (value = start time).
# A resume APPENDS to the transcript, so two concurrent resumes would
# interleave writes — we reject the second (409). Crucially the entry is
# cleared when the claude PROCESS exits, NOT when the HTTP request ends,
# so it survives a client disconnect: a dropped mobile client can poll
# GET /sessions/{id}/resume/status to learn the turn is still running and
# catch up from the transcript. This is the bridge's "activeExecution".
_running_resumes: dict[str, float] = {}
_running_guard = threading.Lock()


def _try_start_resume(session_id: str) -> bool:
    """Atomically mark a resume as running; False if one already is."""
    with _running_guard:
        if session_id in _running_resumes:
            return False
        _running_resumes[session_id] = time.time()
        return True


def _end_resume(session_id: str) -> None:
    with _running_guard:
        _running_resumes.pop(session_id, None)


def _resume_started_at(session_id: str) -> float | None:
    with _running_guard:
        return _running_resumes.get(session_id)


# ── Server-side send queue ────────────────────────────────────────────
# Messages sent while a turn is in flight are queued HERE (not on the
# phone), so the conversation keeps advancing even if the app is locked
# or killed. A per-session daemon worker drains the queue: it waits for
# the session to be free, then runs the next message with
# `claude --resume` (blocking) and repeats. The client just polls status
# and pulls the transcript when it comes back.
# Queue items are {"message": str (footer already applied),
# "images": list[ImageAttachment], "preview": str}.
_queues: dict[str, list[dict]] = {}
_workers: set[str] = set()
_queue_guard = threading.Lock()


def _preview(message: str, images: list, files: list) -> str:
    """One-line queue preview: the user's text, or an attachment marker."""
    if message:
        return message
    parts = []
    if images:
        parts.append(f"{len(images)} image(s)")
    if files:
        parts.append(f"{len(files)} file(s)")
    return f"📎 {', '.join(parts)}"


def _queued(session_id: str) -> list[str]:
    """Message previews for status (attachment-only turns show a marker)."""
    with _queue_guard:
        return [it["preview"] for it in _queues.get(session_id, ())]


def _drain_queue(session_id: str, cwd: str) -> None:
    """Process a session's queued messages one at a time, in order. Runs
    on its own daemon thread so it outlives the enqueue request."""
    while True:
        # Deregister atomically with the empty check so a concurrent
        # enqueue either sees us still registered (and we'll loop back to
        # process its message) or starts a fresh worker.
        with _queue_guard:
            if not _queues.get(session_id):
                _queues.pop(session_id, None)
                _workers.discard(session_id)
                return

        # Wait until the session is free (a live stream or a prior queued
        # run holds the slot). _try_start_resume claims it atomically.
        while not _try_start_resume(session_id):
            time.sleep(0.3)

        with _queue_guard:
            pending = _queues.get(session_id) or []
            item = pending.pop(0) if pending else None
        if item is None:
            _end_resume(session_id)
            continue

        try:
            model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
            job = job_manager.create(kind="resume_queue")
            log.info("resume_queue.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
            if item["images"]:
                # Images can't go on the CLI — feed via stream-json stdin.
                args = claude_runner.build_claude_stdin_args(
                    model=model, output_format="stream-json",
                    extra_flags=["--verbose", "--resume", session_id],
                )
                claude_runner.run_blocking_stdin(
                    args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
                    job_id=job.job_id, stdin_data=_stdin_message(item["message"], item["images"]),
                )
            else:
                args = claude_runner.build_claude_args(
                    prompt=item["message"], model=model, output_format="json",
                    extra_flags=["--resume", session_id],
                )
                claude_runner.run_blocking(
                    args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS, job_id=job.job_id
                )
        except Exception as err:  # a bad turn must not kill the worker
            log.error("resume_queue.failed", session_id=session_id, error=str(err))
        finally:
            _end_resume(session_id)


def _enqueue(session_id: str, cwd: str, message: str, images: list, preview: str) -> int:
    """Append a message (+ images) and ensure a drain worker is running.
    ``message`` already carries the attached-files footer; ``preview`` is
    what /resume/status shows. Returns the new queue depth."""
    with _queue_guard:
        _queues.setdefault(session_id, []).append(
            {"message": message, "images": images, "preview": preview}
        )
        depth = len(_queues[session_id])
        start_worker = session_id not in _workers
        if start_worker:
            _workers.add(session_id)
    if start_worker:
        threading.Thread(target=_drain_queue, args=(session_id, cwd), daemon=True).start()
    return depth


def _extract_reply(stdout: str) -> str:
    """Assistant's final text from claude's output. Handles both
    ``--output-format json`` (one result object) and ``stream-json`` (the
    image path, where we scan for the result line). '' if unexpected — the
    client re-fetches the transcript for the canonical turns anyway."""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data.get("result") or ""
    except (ValueError, TypeError):
        pass
    reply = ""
    for line in stdout.splitlines():
        try:
            evt = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(evt, dict) and evt.get("type") == "result":
            reply = evt.get("result") or reply
    return reply


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
    if not message and not body.images and not body.files:
        raise HTTPException(status_code=422, detail="message, images, or files required")
    # Validate + persist attachments BEFORE claiming the session slot, so
    # a bad file is a clean 422 with nothing half-started.
    message = _with_files_footer(message, _save_files(session_id, body.files))

    if not _try_start_resume(session_id):
        raise HTTPException(status_code=409, detail="this session is already processing a message")
    try:
        model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
        job = job_manager.create(kind="resume_session")
        log.info("resume_session.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
        if body.images:
            args = claude_runner.build_claude_stdin_args(
                model=model, output_format="stream-json", extra_flags=["--verbose", "--resume", session_id]
            )
            result = claude_runner.run_blocking_stdin(
                args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
                job_id=job.job_id, stdin_data=_stdin_message(message, body.images),
            )
        else:
            args = claude_runner.build_claude_args(
                prompt=message, model=model, output_format="json", extra_flags=["--resume", session_id]
            )
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
        _end_resume(session_id)


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
    images = body.images
    if not message and not images and not body.files:
        raise HTTPException(status_code=422, detail="message, images, or files required")
    # Saved (and validated) before streaming starts: attachment problems
    # surface as a proper HTTP 422, not a mid-stream error event.
    message = _with_files_footer(message, _save_files(session_id, body.files))

    def gen() -> Iterator[str]:
        if not _try_start_resume(session_id):
            yield _sse("error", {"message": "this session is already processing a message"})
            return
        spawned = False
        try:
            model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
            job = job_manager.create(kind="resume_session_stream")
            log.info("resume_stream.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
            # Images can't ride the CLI — feed the message as a stream-json
            # user turn (text + image blocks) on stdin. Text-only stays on
            # the -p positional (cheaper, unchanged).
            if images:
                args = claude_runner.build_claude_stdin_args(
                    model=model, output_format="stream-json",
                    extra_flags=["--verbose", "--include-partial-messages", "--resume", session_id],
                )
                ctx = claude_runner.streaming_subprocess_stdin(
                    args, cwd=cwd, job_id=job.job_id, stdin_data=_stdin_message(message, images)
                )
            else:
                args = claude_runner.build_claude_args(
                    prompt=message, model=model, output_format="stream-json",
                    extra_flags=["--verbose", "--include-partial-messages", "--resume", session_id],
                )
                ctx = claude_runner.streaming_subprocess(args, cwd=cwd, job_id=job.job_id)
            try:
                with ctx as proc:
                    # Read claude's stdout on a thread into a queue so the
                    # generator can emit an SSE heartbeat during the long
                    # SILENT phase (resuming a big session loads/replays the
                    # transcript before any token). Without heartbeats an
                    # idle minutes-long connection gets dropped by cellular
                    # NAT / ngrok, which the client would misread as "done".
                    lines: "queue.Queue" = queue.Queue()
                    STDOUT_EOF = object()

                    def _reader() -> None:
                        try:
                            for raw in proc.stdout:
                                lines.put(raw)
                        finally:
                            lines.put(STDOUT_EOF)
                            # Cleared when CLAUDE exits, not when the HTTP
                            # request ends — so a disconnected client can
                            # poll status and see the turn is still running.
                            _end_resume(session_id)

                    threading.Thread(target=_reader, daemon=True).start()
                    spawned = True

                    while True:
                        try:
                            raw = lines.get(timeout=10)
                        except queue.Empty:
                            yield ": keepalive\n\n"  # SSE comment — clients ignore it
                            continue
                        if raw is STDOUT_EOF:
                            break
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
            # On the normal / client-disconnect path the reader's finally
            # clears the marker when claude exits. Only clear here if the
            # reader never started (early failure / spawn error).
            if not spawned:
                _end_resume(session_id)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/resume/queue")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def enqueue_resume(request: Request, session_id: str, body: ResumeRequest) -> dict[str, Any]:
    """Queue a message to continue the session. Unlike /resume/stream this
    does NOT block on a live connection — a background worker runs it (and
    any other queued messages, in order) even if the phone locks or the app
    is killed. The client polls /resume/status and pulls the transcript on
    return. Returns the new queue depth."""
    card = session_index.get_card(session_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    cwd = card.get("cwd")
    if not cwd:
        raise HTTPException(status_code=422, detail="session has no recorded cwd; cannot resume")
    message = body.message.strip()
    if not message and not body.images and not body.files:
        raise HTTPException(status_code=422, detail="message, images, or files required")
    preview = _preview(message, body.images, body.files)
    message = _with_files_footer(message, _save_files(session_id, body.files))

    depth = _enqueue(session_id, cwd, message, body.images, preview)
    return {"ok": True, "session_id": session_id, "queued": depth}


@router.get("/sessions/{session_id}/resume/status")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def resume_status(request: Request, session_id: str) -> dict[str, Any]:
    """In-flight state for the mobile client's catch-up loop (the bridge's
    ``activeExecution``). ``running`` = a turn is generating now; ``queued``
    = messages waiting to run. The client stays in "busy/syncing" and keeps
    pulling the transcript while ``running or queued``; when BOTH are empty
    the conversation has fully advanced. Cheap in-memory read — safe to
    poll."""
    started = _resume_started_at(session_id)
    queued = _queued(session_id)
    return {
        "running": started is not None,
        "started_at": started,
        "queued": queued,
        "queue_count": len(queued),
    }


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
