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
    ANOTHER_BRIDGE_MAX_RUNNING_TURNS,
    ANOTHER_CODER_ATTACHMENTS_DIR,
    ANOTHER_CODER_RATE_LIMIT_SESSIONS,
    ANOTHER_CODER_RESUME_MODEL,
    ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
)
from jobs import job_manager
from services import claude_runner, live_runs, terminal_activity
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


# About 10 MB decoded per image. The whole request is also capped at
# 40 MB by services/guard.py.
MAX_IMAGE_B64_CHARS = 14_000_000


class ImageAttachment(BaseModel):
    """A base64 image the phone attached to a message (raw base64, no
    ``data:`` prefix)."""

    media_type: str = Field(pattern=r"^image/(png|jpeg|jpg|webp|gif)$")
    data: str = Field(min_length=1, max_length=MAX_IMAGE_B64_CHARS)


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
    message / images / files must be present.

    ``voice`` marks a conversation-mode turn: the run switches to
    ``--output-format json --json-schema`` so the reply is GUARANTEED to
    carry a short spoken summary (the ``speech`` field) alongside the
    full text. The tradeoff is no token streaming for that turn."""

    message: str = Field(default="", max_length=100_000)
    images: list[ImageAttachment] = Field(default_factory=list, max_length=10)
    files: list[FileAttachment] = Field(default_factory=list, max_length=MAX_FILES)
    voice: bool = False
    # "Send anyway": run now even if the session is working in a terminal
    # tab or the running-turn cap is reached.
    force: bool = False


# Schema for voice turns. Brevity and the no-code rule live in the field
# descriptions because the structured-output validator does not support
# minLength/maxLength constraints.
VOICE_SPEECH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": "The full answer, exactly as it would normally be written.",
        },
        "speech": {
            "type": "string",
            "description": (
                "One or two short spoken sentences summarizing the reply for "
                "audio. No code, no diffs, no file paths, no markdown. Written "
                "in the same language as the user's message."
            ),
        },
    },
    "required": ["reply", "speech"],
    "additionalProperties": False,
}
VOICE_SPEECH_SCHEMA_JSON = json.dumps(VOICE_SPEECH_SCHEMA)


# A turn driven from the phone must NEVER do voice I/O on the Mac: if the
# resumed session has the VoiceMode MCP loaded (a desktop voice chat), the
# model would otherwise speak through the Mac's speakers and wait on the
# Mac's microphone. The phone is the only voice surface for mobile turns.
MAC_VOICE_TOOLS = "mcp__plugin_voicemode_voicemode__*,mcp__voicemode__*"


def _resume_flags(session_id: str) -> list[str]:
    """Base flags for every mobile-driven resume."""
    return ["--resume", session_id, "--disallowed-tools", MAC_VOICE_TOOLS]


def _voice_flags(session_id: str) -> list[str]:
    """Extra flags for a schema-guaranteed voice turn."""
    return _resume_flags(session_id) + ["--json-schema", VOICE_SPEECH_SCHEMA_JSON]


def _voice_result(stdout: str) -> tuple[str, str | None]:
    """(reply, speech) out of a ``--output-format json`` voice run. Falls
    back to the plain result text with no speech when the structured
    field is missing (older CLI / model ignored the schema)."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return _extract_reply(stdout), None
    if not isinstance(data, dict):
        return _extract_reply(stdout), None
    structured = data.get("structured_output")
    if isinstance(structured, dict) and structured.get("speech"):
        return (
            str(structured.get("reply") or data.get("result") or ""),
            str(structured["speech"]),
        )
    return str(data.get("result") or ""), None


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


def _mark_running_locked(session_id: str) -> None:
    _running_resumes[session_id] = time.time()
    live_runs.start(session_id)


def _try_start_resume(session_id: str) -> bool:
    """Atomically mark a resume as running; False if one already is.
    Also opens the turn's live run, so a phone can attach to it. No
    terminal or cap checks: ``_claim`` is the gate for new turns."""
    with _running_guard:
        if session_id in _running_resumes:
            return False
        _mark_running_locked(session_id)
        return True


def _terminal(session_id: str) -> "terminal_activity.TerminalState | None":
    """Another live Claude Code copy of this session (a terminal tab)."""
    return terminal_activity.watch.state(session_id, session_index.transcript_path(session_id))


def _claim(session_id: str, *, force: bool = False) -> str | None:
    """Claim the session for a new turn. None when claimed, else why not:

    - ``"busy"``: a bridge turn already runs on this session.
    - ``"terminal"``: the session is working in a terminal tab. A second
      copy would redo its work (October 27, 2026, the GR00T script).
    - ``"limit"``: ANOTHER_BRIDGE_MAX_RUNNING_TURNS turns already run.

    ``force`` ("Send anyway") skips the terminal and cap checks, never the
    busy one, which protects the transcript."""
    if not force:
        state = _terminal(session_id)
        if state is not None and state.active:
            return "terminal"
    with _running_guard:
        if session_id in _running_resumes:
            return "busy"
        if not force and len(_running_resumes) >= ANOTHER_BRIDGE_MAX_RUNNING_TURNS:
            return "limit"
        _mark_running_locked(session_id)
        return None


# What /resume/blocking callers and SSE clients are told for each reason.
_HOLD_MESSAGES = {
    "busy": "this session is already processing a message",
    "terminal": "this session is working in a terminal on the Mac",
    "limit": f"{ANOTHER_BRIDGE_MAX_RUNNING_TURNS} phone turns are already running",
}


def _end_resume(session_id: str) -> None:
    # Under the same lock as the start, so a queued turn claiming the
    # session right after can't have its new live run closed by mistake.
    with _running_guard:
        _running_resumes.pop(session_id, None)
        live_runs.finish(session_id)


def _live_run(session_id: str) -> live_runs.LiveRun:
    """The live run this turn just opened (a detached one if it is gone)."""
    return live_runs.get(session_id) or live_runs.LiveRun()


def _publish_line(run: live_runs.LiveRun, raw: str) -> None:
    """One stream-json line from claude into the turn's live events: text
    deltas as they arrive, then each finished message's tool steps (its
    text was already streamed by the deltas)."""
    line = raw.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    chunk = _text_delta(event)
    if chunk:
        run.publish("text", {"chunk": chunk})
        return
    if event.get("type") == "assistant":
        _, tools = _assistant_blocks(event.get("message", {}).get("content"))
        for t in tools:
            run.publish("tool", {"name": t.name, "label": t.label, "stat": t.stat})
        run.end_message()


def _follow_sse(run: live_runs.LiveRun, start: int | None = None) -> Iterator[str]:
    """A live run as SSE frames, with a keepalive comment every 10 quiet
    seconds so cellular NAT and ngrok keep the connection open."""
    for item in run.follow(start):
        if item is None:
            yield ": keepalive\n\n"  # SSE comment, clients ignore it
        else:
            yield _sse(*item)


def _spawn_streaming(session_id: str, cwd: str, message: str, images: list, job_id: str):
    """Start ``claude --resume`` with token streaming. Images can't ride
    the CLI, so they go as a stream-json user turn on stdin; text-only
    stays on the -p positional. Returns the subprocess context manager."""
    model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
    flags = ["--verbose", "--include-partial-messages"] + _resume_flags(session_id)
    if images:
        args = claude_runner.build_claude_stdin_args(
            model=model, output_format="stream-json", extra_flags=flags
        )
        return claude_runner.streaming_subprocess_stdin(
            args, cwd=cwd, job_id=job_id, stdin_data=_stdin_message(message, images)
        )
    args = claude_runner.build_claude_args(
        prompt=message, model=model, output_format="stream-json", extra_flags=flags
    )
    return claude_runner.streaming_subprocess(args, cwd=cwd, job_id=job_id)


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
# "images": list[ImageAttachment], "preview": str, "voice": bool,
# "force": bool}. The worker also waits while the session works in a
# terminal tab or the running-turn cap is reached; ``_holds`` records why,
# for /resume/status.
_queues: dict[str, list[dict]] = {}
_workers: set[str] = set()
_holds: dict[str, str] = {}
_queue_guard = threading.Lock()

# How often a held queue checks the terminal and the cap again.
HOLD_POLL_SECONDS = 2.0


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
                _holds.pop(session_id, None)
                return
            force = bool(_queues[session_id][0].get("force"))

        # Wait until the session is free: no bridge turn on it, not
        # working in a terminal tab, and a slot under the cap. _claim
        # takes it atomically. "Send anyway" sets force on the items.
        reason = _claim(session_id, force=force)
        if reason is not None:
            with _queue_guard:
                if reason == "busy":
                    _holds.pop(session_id, None)
                else:
                    _holds[session_id] = reason
            time.sleep(0.3 if reason == "busy" else HOLD_POLL_SECONDS)
            continue

        with _queue_guard:
            _holds.pop(session_id, None)
            pending = _queues.get(session_id) or []
            item = pending.pop(0) if pending else None
        if item is None:
            _end_resume(session_id)
            continue

        try:
            job = job_manager.create(kind="resume_queue")
            log.info("resume_queue.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
            if item.get("voice"):
                _run_queued_voice(session_id, cwd, item, job.job_id)
            else:
                # Streamed like a live send, so a phone can attach and watch
                # it, with no 300 s cut (ticket 8 adds one deadline for all).
                run = _live_run(session_id)
                with _spawn_streaming(
                    session_id, cwd, item["message"], item["images"], job.job_id
                ) as proc:
                    for raw in proc.stdout:
                        _publish_line(run, raw)
        except Exception as err:  # a bad turn must not kill the worker
            log.error("resume_queue.failed", session_id=session_id, error=str(err))
        finally:
            _end_resume(session_id)


def _run_queued_voice(session_id: str, cwd: str, item: dict, job_id: str) -> None:
    """A queued voice turn: blocking json output for the schema guarantee,
    so it has no live events (an attached phone just waits for done)."""
    model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
    if item["images"]:
        args = claude_runner.build_claude_stdin_args(
            model=model, output_format="json", extra_flags=_voice_flags(session_id)
        )
        claude_runner.run_blocking_stdin(
            args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
            job_id=job_id, stdin_data=_stdin_message(item["message"], item["images"]),
        )
    else:
        args = claude_runner.build_claude_args(
            prompt=item["message"], model=model, output_format="json",
            extra_flags=_voice_flags(session_id),
        )
        claude_runner.run_blocking(
            args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS, job_id=job_id
        )


def _enqueue(
    session_id: str, cwd: str, message: str, images: list, preview: str,
    voice: bool = False, force: bool = False,
) -> int:
    """Append a message (+ images) and ensure a drain worker is running.
    ``message`` already carries the attached-files footer; ``preview`` is
    what /resume/status shows. Returns the new queue depth."""
    with _queue_guard:
        _queues.setdefault(session_id, []).append(
            {"message": message, "images": images, "preview": preview, "voice": voice, "force": force}
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
    # Each card says whether a terminal tab holds the session and is
    # working, so the list can show it before Ken opens the chat.
    records = terminal_activity.watch.records()
    for card in sessions:
        state = None
        if card["session_id"] in records:
            state = terminal_activity.watch.state(
                card["session_id"], session_index.transcript_path(card["session_id"]), records=records
            )
        card["terminal"] = state.as_dict() if state else None
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

    # A session open but quiet in a terminal tab is resumed anyway: the tab
    # catches up from the transcript (its sync hook). One that is WORKING
    # there is refused (409), since a second copy would redo its work.

    message = body.message.strip()
    if not message and not body.images and not body.files:
        raise HTTPException(status_code=422, detail="message, images, or files required")
    # Validate + persist attachments BEFORE claiming the session slot, so
    # a bad file is a clean 422 with nothing half-started.
    message = _with_files_footer(message, _save_files(session_id, body.files))

    reason = _claim(session_id, force=body.force)
    if reason is not None:
        raise HTTPException(status_code=409, detail=_HOLD_MESSAGES[reason])
    try:
        model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
        job = job_manager.create(kind="resume_session")
        log.info("resume_session.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
        if body.images:
            args = claude_runner.build_claude_stdin_args(
                model=model,
                output_format="json" if body.voice else "stream-json",
                extra_flags=_voice_flags(session_id) if body.voice
                else ["--verbose"] + _resume_flags(session_id),
            )
            result = claude_runner.run_blocking_stdin(
                args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
                job_id=job.job_id, stdin_data=_stdin_message(message, body.images),
            )
        else:
            args = claude_runner.build_claude_args(
                prompt=message, model=model, output_format="json",
                extra_flags=_voice_flags(session_id) if body.voice else _resume_flags(session_id),
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

        if body.voice:
            reply, speech = _voice_result(result.stdout)
            return {"ok": True, "session_id": session_id, "reply": reply, "speech": speech}
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
        event: queued data: {"reason": "terminal" | "limit", "queued": n}

    404 unknown id (raised before streaming). Concurrency is guarded by
    the same per-session lock as the blocking endpoint — a second send
    gets an `error` event rather than corrupting the transcript. When the
    session is working in a terminal tab, or the running-turn cap is
    reached, the message goes to the queue instead (``queued``, then
    ``done``) and runs by itself once that clears. ``force`` skips both."""
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
    preview = _preview(message, images, body.files)
    # Saved (and validated) before streaming starts: attachment problems
    # surface as a proper HTTP 422, not a mid-stream error event.
    message = _with_files_footer(message, _save_files(session_id, body.files))

    if body.voice:
        return _voice_stream_response(session_id, cwd, message, images, preview, body.force)

    def gen() -> Iterator[str]:
        reason = _claim(session_id, force=body.force)
        if reason == "busy":
            yield _sse("error", {"message": _HOLD_MESSAGES[reason]})
            return
        if reason is not None:
            yield from _queued_sse(session_id, cwd, message, images, preview, reason, voice=False)
            return
        run = _live_run(session_id)
        spawned = False
        try:
            job = job_manager.create(kind="resume_session_stream")
            log.info("resume_stream.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id)
            try:
                with _spawn_streaming(session_id, cwd, message, images, job.job_id) as proc:
                    # Claude's stdout is read on its own thread into the
                    # turn's live run, so the turn keeps publishing after
                    # this client disconnects and a phone can attach again.
                    def _reader() -> None:
                        try:
                            for raw in proc.stdout:
                                _publish_line(run, raw)
                        finally:
                            # Cleared when CLAUDE exits, not when the HTTP
                            # request ends, so status still says running.
                            _end_resume(session_id)

                    threading.Thread(target=_reader, daemon=True).start()
                    spawned = True
                    # The whole turn from its first event, with keepalives
                    # during the long silent start of a big session.
                    yield from _follow_sse(run, start=0)
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


def _queued_sse(
    session_id: str, cwd: str, message: str, images: list, preview: str, reason: str, *, voice: bool
) -> Iterator[str]:
    """Queue a held message and tell the phone why, instead of starting a
    second copy of a session that is working in a terminal tab (or going
    past the running-turn cap). The queue runs it once that clears."""
    depth = _enqueue(session_id, cwd, message, images, preview, voice=voice)
    log.info("resume_stream.held", session_id=session_id, reason=reason, queued=depth)
    yield _sse("queued", {"reason": reason, "queued": depth, "message": _HOLD_MESSAGES[reason]})
    yield _sse("done", {"session_id": session_id})


def _voice_stream_response(
    session_id: str, cwd: str, message: str, images: list[ImageAttachment],
    preview: str, force: bool = False,
) -> StreamingResponse:
    """Voice-mode variant of the resume stream. The run is blocking (the
    schema guarantee is incompatible with token streaming), so the claude
    process runs on a worker thread while the generator emits keepalives;
    when it finishes we emit the full reply as one ``text`` chunk, then a
    ``speech`` event with the guaranteed summary, then ``done``. The same
    per-session lock and disconnect semantics apply: the worker clears the
    in-flight marker when the PROCESS exits, not when the request ends."""

    def gen() -> Iterator[str]:
        reason = _claim(session_id, force=force)
        if reason == "busy":
            yield _sse("error", {"message": _HOLD_MESSAGES[reason]})
            return
        if reason is not None:
            yield from _queued_sse(session_id, cwd, message, images, preview, reason, voice=True)
            return
        outcome: "queue.Queue" = queue.Queue()

        def _worker() -> None:
            try:
                model = session_index.latest_model(session_id) or ANOTHER_CODER_RESUME_MODEL
                job = job_manager.create(kind="resume_session_voice")
                log.info(
                    "resume_voice.spawning", session_id=session_id, cwd=cwd, job_id=job.job_id
                )
                if images:
                    args = claude_runner.build_claude_stdin_args(
                        model=model, output_format="json", extra_flags=_voice_flags(session_id)
                    )
                    result = claude_runner.run_blocking_stdin(
                        args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
                        job_id=job.job_id, stdin_data=_stdin_message(message, images),
                    )
                else:
                    args = claude_runner.build_claude_args(
                        prompt=message, model=model, output_format="json",
                        extra_flags=_voice_flags(session_id),
                    )
                    result = claude_runner.run_blocking(
                        args, cwd=cwd, timeout_seconds=ANOTHER_CODER_RESUME_TIMEOUT_SECONDS,
                        job_id=job.job_id,
                    )
                outcome.put(result)
            except Exception as err:  # surfaced as an SSE error below
                outcome.put(err)
            finally:
                _end_resume(session_id)

        threading.Thread(target=_worker, daemon=True).start()
        while True:
            try:
                result = outcome.get(timeout=10)
                break
            except queue.Empty:
                yield ": keepalive\n\n"

        if isinstance(result, Exception):
            yield _sse("error", {"message": f"claude failed: {result}"})
            return
        if result.spawn_error:
            yield _sse("error", {"message": f"could not spawn claude: {result.spawn_error}"})
            return
        if result.timed_out:
            yield _sse(
                "error",
                {
                    "message": f"claude timed out after {ANOTHER_CODER_RESUME_TIMEOUT_SECONDS}s "
                    "(the turn may be partially written; refresh to see it)"
                },
            )
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            yield _sse("error", {"message": f"claude exited {result.returncode}: {detail}"})
            return

        reply, speech = _voice_result(result.stdout)
        if reply:
            yield _sse("text", {"chunk": reply})
        if speech:
            yield _sse("speech", {"speech": speech})
        yield _sse("done", {"session_id": session_id})

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

    depth = _enqueue(
        session_id, cwd, message, body.images, preview, voice=body.voice, force=body.force
    )
    return {"ok": True, "session_id": session_id, "queued": depth}


@router.post("/sessions/{session_id}/resume/queue/release")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def release_queue(request: Request, session_id: str) -> dict[str, Any]:
    """"Send anyway": the session's queued messages stop waiting for the
    terminal tab and the running-turn cap. They still run one at a time,
    after any bridge turn already running on this session."""
    with _queue_guard:
        items = _queues.get(session_id) or []
        for item in items:
            item["force"] = True
    log.info("resume_queue.released", session_id=session_id, count=len(items))
    return {"ok": True, "session_id": session_id, "released": len(items)}


@router.get("/sessions/{session_id}/resume/attach")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def attach_resume(request: Request, session_id: str):
    """Watch a running turn again after the phone left the chat or iOS
    dropped its stream. Fetch ``/messages`` first: finished messages are
    in the transcript, so this replays only the message claude is writing
    now, then streams the rest of the turn live with the same ``text`` and
    ``tool`` events as /resume/stream, keepalives while quiet, and
    ``done`` when the turn ends. Queued turns stream here too; voice and
    blocking turns only send ``done``. Nothing running: ``done`` at once."""
    run = live_runs.get(session_id)

    def gen() -> Iterator[str]:
        if run is not None:
            yield from _follow_sse(run)
        yield _sse("done", {"session_id": session_id})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/resume/status")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def resume_status(request: Request, session_id: str) -> dict[str, Any]:
    """In-flight state for the mobile client's catch-up loop (the bridge's
    ``activeExecution``). ``running`` = a turn is generating now; ``queued``
    = messages waiting to run; ``held`` = why they wait; ``terminal`` = a
    terminal tab holding the session and whether it is working. The client stays in "busy/syncing" and keeps
    pulling the transcript while ``running or queued``; when BOTH are empty
    the conversation has fully advanced. Cheap in-memory read — safe to
    poll."""
    started = _resume_started_at(session_id)
    queued = _queued(session_id)
    with _queue_guard:
        held = _holds.get(session_id) if queued else None
    state = _terminal(session_id)
    return {
        "running": started is not None,
        "started_at": started,
        "queued": queued,
        "queue_count": len(queued),
        # Why queued messages wait: "terminal" (working in a terminal tab)
        # or "limit" (the running-turn cap), else None.
        "held": held,
        # Another live copy of the session, usually a terminal tab.
        "terminal": state.as_dict() if state else None,
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
