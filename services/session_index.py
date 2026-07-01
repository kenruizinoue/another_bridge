"""Read-only index over Claude Code's on-disk conversation transcripts.

Claude Code writes one JSONL file per conversation under
``<projects_dir>/<encoded-cwd>/<sessionId>.jsonl`` (append-only, one
JSON event per line). This module turns that tree into card-shaped
metadata for the mobile session picker — WITHOUT resuming or mutating
anything. It is the ``GET /sessions`` data source.

Two properties matter for a 1,000+ file home directory:

  * **Cheap list sort.** Last-activity ordering uses the file's mtime,
    not a full parse — the OS already tracks it and an append bumps it,
    so it's an accurate "last touched" proxy for free.

  * **mtime-keyed cache.** Parsing a transcript for its title / cwd /
    message count means reading the file. We cache the parsed card
    keyed by (path, mtime, size); an unchanged file is never re-read,
    so a pull-to-refresh on the phone re-scans directory stat()s only.
    A live session whose file grows invalidates its own entry via the
    changed mtime, so cards stay fresh without a TTL.

Nothing here writes to disk or spawns a process. Resuming a session
(``claude --resume <id>``) is a separate, state-changing concern that
belongs in the runner, not the index.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

from config import CLAUDE_PROJECTS_DIR


def _pid_alive(pid: int) -> bool:
    """True if the process exists. os.kill(pid, 0) raises ProcessLookupError
    when it's gone and PermissionError when it exists but isn't ours."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def is_session_live(session_id: str) -> bool:
    """True if a Claude Code process currently owns this session. Claude
    tracks running sessions in ``<claude-config>/sessions/*.json`` (pid +
    sessionId). Resuming a session that a terminal still has open would
    mean two processes appending to one transcript, so the resume endpoint
    uses this to refuse (409) rather than corrupt the file."""
    sessions_dir = CLAUDE_PROJECTS_DIR.parent / "sessions"
    if not sessions_dir.exists():
        return False
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if data.get("sessionId") == session_id:
            pid = data.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                return True
    return False

# Title fallbacks stop at the first line that yields real text. A user
# turn's content is either a plain string or a list of blocks (text +
# tool_result + attachments); we only want human-typed text, so
# tool_result / non-text blocks are skipped. Command stubs that Claude
# Code injects (slash-command expansions, caveats) start with these
# markers and would make a useless card title.
_TITLE_MAX = 120
# Harness-injected user turns that are NOT human messages: slash-command
# expansions, caveats, local-command output, background task-notifications,
# and system-reminders. These arrive as user-role turns (some even share
# promptSource "sdk" with real mobile messages), so we discriminate on the
# content wrapper rather than promptSource — otherwise mobile turns, which
# are plain text, would be filtered too.
_SKIP_TEXT_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command",
    "<task-notification>",
    "<system-reminder>",
    "Caveat:",
)


@dataclass(frozen=True)
class SessionCard:
    """One row in the mobile picker. ``session_id`` is the filename
    stem (identical to the ``sessionId`` field inside the transcript),
    which is exactly what ``claude --resume <session_id>`` expects. The
    ``cwd`` is the directory the conversation was born in — the runner
    MUST spawn ``claude`` there for ``--resume`` to find the file."""

    session_id: str
    title: str
    cwd: Optional[str]
    project: str  # human label — basename of cwd, else the encoded dir
    message_count: int  # human turns + assistant turns only
    created_at: Optional[str]  # ISO8601 from the first event, if present
    last_activity: float  # file mtime, epoch seconds (list sort key)
    size_bytes: int


def _first_text(content) -> Optional[str]:
    """Pull human-readable text out of a user message's content, which
    is either a string or a list of typed blocks. Returns None when the
    turn carries no real text (e.g. a pure tool_result echo)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if parts:
            return "\n".join(parts)
    return None


def _clean_title(text: str) -> Optional[str]:
    text = text.strip()
    if not text or text.startswith(_SKIP_TEXT_PREFIXES):
        return None
    collapsed = " ".join(text.split())
    return collapsed[:_TITLE_MAX] if collapsed else None


def _parse_transcript(path: Path) -> Optional[SessionCard]:
    """Full single-pass parse of one JSONL transcript into a card.
    Returns None for an empty / unreadable / contentless file so the
    caller can drop it rather than surface a blank card.

    Title precedence: the ``ai-title`` event Claude Code generates >
    a ``summary`` record > the first real user message > a fallback.
    We scan the whole file because ``ai-title`` is emitted after the
    first turn, but the files are small and the result is cached."""
    ai_title: Optional[str] = None
    summary: Optional[str] = None
    first_user_text: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    message_count = 0

    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue  # tolerate a torn final line from a live append
                if not isinstance(event, dict):
                    continue

                etype = event.get("type")
                if created_at is None and event.get("timestamp"):
                    created_at = event.get("timestamp")
                if cwd is None and event.get("cwd"):
                    cwd = event.get("cwd")

                if etype == "ai-title" and not ai_title:
                    ai_title = event.get("aiTitle") or event.get("title")
                elif etype == "summary" and not summary:
                    summary = event.get("summary")
                elif etype == "user":
                    # Sidechain = subagent transcript; isMeta = injected
                    # image/caveat metadata. Neither is a human turn, so
                    # both are excluded from the count and the title.
                    if event.get("isSidechain") or event.get("isMeta"):
                        continue
                    message_count += 1
                    if first_user_text is None:
                        raw = _first_text(event.get("message", {}).get("content"))
                        if raw:
                            first_user_text = raw
                elif etype == "assistant":
                    if not event.get("isSidechain"):
                        message_count += 1
    except OSError:
        return None

    title = (
        (ai_title and _clean_title(ai_title))
        or (summary and _clean_title(summary))
        or (first_user_text and _clean_title(first_user_text))
        or "(untitled session)"
    )
    if message_count == 0 and title == "(untitled session)":
        return None  # metadata-only stub (e.g. a session that never got a turn)

    project = Path(cwd).name if cwd else path.parent.name
    stat = path.stat()
    return SessionCard(
        session_id=path.stem,
        title=title,
        cwd=cwd,
        project=project or path.parent.name,
        message_count=message_count,
        created_at=created_at,
        last_activity=stat.st_mtime,
        size_bytes=stat.st_size,
    )


@dataclass(frozen=True)
class Turn:
    """One renderable line in the conversation view. The transcript is
    an event log (tool calls, tool results, thinking, snapshots), so a
    turn is the filtered human-visible slice of it:

      role 'user'      → text you typed
      role 'assistant' → Claude's text reply (``tool_calls`` = tools it
                         invoked in the same message, shown as a marker)
      role 'tool'      → an assistant step that was ONLY tool calls /
                         thinking (no text); rendered as a dim marker

    ``index`` is a stable 0-based position among EMITTED turns, counted
    from the start of the session. Appends only add higher indices, so
    it is a safe cursor for backward (older) pagination even while a
    live session grows."""

    index: int
    uuid: Optional[str]
    role: str
    text: Optional[str]
    tool_calls: int  # == len(tools); kept for a quick count without the list
    tools: list  # list[ToolRef] — per-tool detail lines for the view
    timestamp: Optional[str]


@dataclass(frozen=True)
class ToolRef:
    """A compact, human-readable summary of one tool call — the mobile
    analogue of the terminal's ``Update(.env.local)  +1 -1`` line. Built
    from the ``tool_use`` block's inputs alone (no result correlation),
    so it stays cheap. ``stat`` is a diff delta for edits, else None."""

    name: str  # raw tool name (Edit, Bash, Read, …)
    label: str  # "Update(.env.local)", "Bash: restart expo", "Read(api.ts)"
    stat: Optional[str]  # "+3 -1" for edits/writes, else None


def _basename(path: Optional[str]) -> Optional[str]:
    return path.rsplit("/", 1)[-1] if path else path


def _line_count(s: Optional[str]) -> int:
    if not s:
        return 0
    return s.count("\n") + 1


def _tool_ref(block: dict) -> ToolRef:
    """Summarize one tool_use block. Mirrors how Claude Code labels tools
    in the terminal so the mobile view reads the same way."""
    name = block.get("name") or "tool"
    inp = block.get("input") or {}

    if name in ("Edit", "MultiEdit"):
        f = _basename(inp.get("file_path"))
        if name == "MultiEdit":
            n = len(inp.get("edits") or [])
            return ToolRef(name, f"Update({f})" if f else "Update", f"{n} edits")
        removed = _line_count(inp.get("old_string"))
        added = _line_count(inp.get("new_string"))
        return ToolRef(name, f"Update({f})" if f else "Update", f"+{added} -{removed}")
    if name == "Write":
        f = _basename(inp.get("file_path"))
        return ToolRef(name, f"Write({f})" if f else "Write", f"+{_line_count(inp.get('content'))}")
    if name == "Read":
        f = _basename(inp.get("file_path"))
        return ToolRef(name, f"Read({f})" if f else "Read", None)
    if name == "Bash":
        desc = inp.get("description") or (inp.get("command") or "").strip().splitlines()[:1]
        desc = desc[0] if isinstance(desc, list) else desc
        desc = (desc or "")[:60]
        return ToolRef(name, f"Bash: {desc}" if desc else "Bash", None)
    if name in ("Grep", "Glob"):
        pat = inp.get("pattern") or inp.get("query") or ""
        return ToolRef(name, f"{name}({pat})" if pat else name, None)
    if name == "Task":
        t = inp.get("subagent_type") or inp.get("description") or ""
        return ToolRef(name, f"Task({t})" if t else "Task", None)
    return ToolRef(name, name, None)


def _assistant_blocks(content) -> tuple[Optional[str], list[ToolRef]]:
    """Return (joined text, tool summaries) for an assistant message's
    content list. Thinking blocks are ignored entirely."""
    if not isinstance(content, list):
        return (content if isinstance(content, str) else None), []
    texts: list[str] = []
    tools: list[ToolRef] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and b.get("text"):
            texts.append(b["text"])
        elif bt == "tool_use":
            tools.append(_tool_ref(b))
    return ("\n".join(texts) if texts else None), tools


def _parse_turns(path: Path) -> list[Turn]:
    """Full single-pass parse of a transcript into the renderable turn
    list (oldest → newest). Skips sidechains, tool-result echoes,
    command stubs, and thinking-only steps. Cached by the caller."""
    turns: list[Turn] = []
    idx = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                # isMeta = harness-injected metadata, NOT a human turn:
                # image descriptors from pasted/read screenshots
                # ("[Image: original …, Multiply coordinates by …]"),
                # caveats, command output. These carry text, so without
                # this guard they render as bogus "you" turns.
                if not isinstance(event, dict) or event.get("isSidechain") or event.get("isMeta"):
                    continue

                etype = event.get("type")
                if etype not in ("user", "assistant"):
                    continue
                content = event.get("message", {}).get("content")
                ts = event.get("timestamp")
                uuid = event.get("uuid")

                if etype == "user":
                    text = _first_text(content)  # None for tool_result echoes
                    if not text or text.startswith(_SKIP_TEXT_PREFIXES):
                        continue
                    turns.append(Turn(idx, uuid, "user", text, 0, [], ts))
                    idx += 1
                else:  # assistant
                    text, tools = _assistant_blocks(content)
                    if text:
                        turns.append(Turn(idx, uuid, "assistant", text, len(tools), tools, ts))
                        idx += 1
                    elif tools:
                        turns.append(Turn(idx, uuid, "tool", None, len(tools), tools, ts))
                        idx += 1
                    # else: thinking-only step → nothing to render
    except OSError:
        return []
    return turns


def _scan_latest_model(path: Path) -> Optional[str]:
    """The model of the most-recent assistant turn (``message.model``).
    Lets a mobile resume continue on the session's own model instead of
    forcing the bridge default. Returns None if no assistant turn records
    a model (older transcripts / never answered)."""
    model: Optional[str] = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "assistant" and not event.get("isSidechain"):
                    msg = event.get("message")
                    if isinstance(msg, dict) and msg.get("model"):
                        model = msg["model"]
    except OSError:
        return None
    return model


class SessionIndex:
    """mtime-cached view over the transcript tree. One instance is a
    module singleton; it holds parsed cards keyed by path and only
    re-parses a file whose (mtime, size) changed since last seen."""

    def __init__(self, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> None:
        self._projects_dir = projects_dir
        # path -> (mtime, size, card). card is None for files that
        # parsed to nothing, so we don't retry them every scan.
        self._cache: dict[str, tuple[float, int, Optional[SessionCard]]] = {}
        # path -> (mtime, size, turns). Separate from _cache because
        # parsing the full turn list is heavier than card metadata, and
        # we only pay it when a conversation is actually opened.
        self._turns_cache: dict[str, tuple[float, int, list[Turn]]] = {}
        # path -> (mtime, size, model). Latest assistant model, cached so
        # a resume doesn't re-scan a large transcript every send.
        self._model_cache: dict[str, tuple[float, int, Optional[str]]] = {}

    def _iter_transcripts(self) -> Iterable[Path]:
        if not self._projects_dir.exists():
            return []
        return self._projects_dir.glob("**/*.jsonl")

    def _card_for(self, path: Path) -> Optional[SessionCard]:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = str(path)
        cached = self._cache.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        card = _parse_transcript(path)
        self._cache[key] = (stat.st_mtime, stat.st_size, card)
        return card

    def list_cards(
        self,
        *,
        query: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Return (page, total) of cards, newest-activity first.

        ``query`` is a case-insensitive substring matched against title,
        project and cwd. ``limit``/``offset`` paginate the FILTERED,
        already-sorted set so the mobile list can scroll without
        re-fetching everything. ``total`` is the filtered count (before
        pagination) so the client can render "showing N of M"."""
        cards: list[SessionCard] = []
        for path in self._iter_transcripts():
            card = self._card_for(path)
            if card is not None:
                cards.append(card)

        if project:
            cards = [c for c in cards if c.project == project]
        if query:
            q = query.lower()
            cards = [
                c
                for c in cards
                if q in c.title.lower()
                or q in c.project.lower()
                or (c.cwd and q in c.cwd.lower())
            ]

        cards.sort(key=lambda c: c.last_activity, reverse=True)
        total = len(cards)
        limit = max(0, min(limit, 500))
        offset = max(0, offset)
        page = cards[offset : offset + limit]
        return [asdict(c) for c in page], total

    def get_card(self, session_id: str) -> Optional[dict]:
        """Single card by session id. Scans the tree for the matching
        filename rather than trusting a cached path, so a session moved
        / created since the last list call still resolves."""
        for path in self._iter_transcripts():
            if path.stem == session_id:
                card = self._card_for(path)
                return asdict(card) if card else None
        return None

    def _path_for(self, session_id: str) -> Optional[Path]:
        for path in self._iter_transcripts():
            if path.stem == session_id:
                return path
        return None

    def latest_model(self, session_id: str) -> Optional[str]:
        """The model the session most recently ran on, or None if unknown.
        mtime-cached so a resume on a huge transcript doesn't re-scan."""
        path = self._path_for(session_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        key = str(path)
        cached = self._model_cache.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        model = _scan_latest_model(path)
        self._model_cache[key] = (stat.st_mtime, stat.st_size, model)
        return model

    def _turns_for(self, path: Path) -> list[Turn]:
        try:
            stat = path.stat()
        except OSError:
            return []
        key = str(path)
        cached = self._turns_cache.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        turns = _parse_turns(path)
        self._turns_cache[key] = (stat.st_mtime, stat.st_size, turns)
        return turns

    def get_messages(
        self,
        session_id: str,
        *,
        before: Optional[int] = None,
        limit: int = 50,
    ) -> Optional[dict]:
        """One page of a conversation, NEWEST-FIRST, for an inverted
        chat list. Returns None when the session id resolves to no file.

        Backward (older) pagination: the client asks for the latest page
        first (``before`` unset), then passes back ``next_before`` to
        walk toward the top. ``before`` is an exclusive upper bound on
        turn index, so a page is turns ``[start, before)`` reversed.

          {"messages": [...newest-first...],
           "total": N, "has_more": bool, "next_before": int|null}
        """
        path = self._path_for(session_id)
        if path is None:
            return None

        turns = self._turns_for(path)
        total = len(turns)
        limit = max(1, min(limit, 200))
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
        page = turns[start:end]

        return {
            "messages": [asdict(t) for t in reversed(page)],
            "total": total,
            "has_more": start > 0,
            "next_before": start if start > 0 else None,
        }


session_index = SessionIndex()
