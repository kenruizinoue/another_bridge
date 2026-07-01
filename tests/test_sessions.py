"""Coverage for the read-only session-browsing surface:

  * ``services.session_index`` — card metadata + renderable-turn parsing
    over a FIXTURE transcript tree (never the developer's real
    ``~/.claude/projects``).
  * ``GET /sessions``, ``/sessions/{id}``, ``/sessions/{id}/messages``.

Like the other router tests, we build a minimal app with only the
sessions router (skips the lifespan probe/reaper and the include-level
auth gate) and monkeypatch a tmp_path-backed ``SessionIndex`` in, so the
suite is deterministic and can't leak real history into assertions.

The fixture transcript deliberately exercises every filtering branch:
a normal user turn, an assistant turn that mixes thinking + text +
tool_use, a tool-only assistant step, a tool_result echo, and a
sidechain — only four of these should survive into the chat view.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sessions as sessions_router
from services.session_index import SessionIndex

CWD = "/Users/dev/proj"


def _events() -> list[dict]:
    return [
        {"type": "mode", "sessionId": "sess-1"},  # non-message noise
        {
            "type": "user",
            "cwd": CWD,
            "timestamp": "2026-06-30T10:00:00Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "peek AnotherAgent frontend"},
        },
        {"type": "ai-title", "aiTitle": "Peek AnotherAgent frontend"},
        {
            "type": "assistant",
            "timestamp": "2026-06-30T10:00:01Z",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal"},  # dropped
                    {"type": "text", "text": "Sure, looking now."},
                    {"type": "tool_use", "name": "Read"},  # counted as 1
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-30T10:00:02Z",
            "uuid": "a2",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Grep"}]},
        },  # tool-only → role 'tool'
        {
            "type": "user",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": []}],
            },
        },  # tool_result echo → dropped
        {
            "type": "assistant",
            "timestamp": "2026-06-30T10:00:03Z",
            "uuid": "a3",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        },
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": "subagent noise"},
        },  # sidechain → dropped
    ]


def _write_session(root: Path, encoded_cwd: str, session_id: str, events: list[dict]) -> Path:
    d = root / encoded_cwd
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def index(tmp_path: Path) -> SessionIndex:
    _write_session(tmp_path, "-Users-dev-proj", "sess-1", _events())
    return SessionIndex(projects_dir=tmp_path)


@pytest.fixture
def client(index: SessionIndex, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(sessions_router, "session_index", index)
    app = FastAPI()
    app.include_router(sessions_router.router)
    return TestClient(app)


# ── Cards ─────────────────────────────────────────────────────────────


class TestSessionCards:
    def test_list_returns_card_with_ai_title(self, client: TestClient) -> None:
        body = client.get("/sessions").json()
        assert body["total"] == 1
        card = body["sessions"][0]
        assert card["session_id"] == "sess-1"
        assert card["title"] == "Peek AnotherAgent frontend"
        assert card["project"] == "proj"  # basename of cwd
        assert card["cwd"] == CWD
        assert card["message_count"] > 0

    def test_search_filters_out_non_matches(self, client: TestClient) -> None:
        assert client.get("/sessions", params={"q": "anotheragent"}).json()["total"] == 1
        assert client.get("/sessions", params={"q": "no-such-topic"}).json()["total"] == 0

    def test_get_card_by_id(self, client: TestClient) -> None:
        resp = client.get("/sessions/sess-1")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Peek AnotherAgent frontend"

    def test_get_card_unknown_id_404(self, client: TestClient) -> None:
        assert client.get("/sessions/nope").status_code == 404


# ── Messages (filtering + pagination) ─────────────────────────────────


class TestSessionMessages:
    def test_filters_to_four_renderable_turns(self, index: SessionIndex) -> None:
        # user(peek) + assistant(text+tool) + tool-only + assistant(Done)
        # thinking, tool_result echo and sidechain are all dropped.
        page = index.get_messages("sess-1")
        assert page["total"] == 4

    def test_latest_page_is_newest_first(self, client: TestClient) -> None:
        body = client.get("/sessions/sess-1/messages", params={"limit": 2}).json()
        assert body["total"] == 4
        assert body["has_more"] is True
        assert body["next_before"] == 2
        newest, second = body["messages"]
        assert newest["index"] == 3
        assert newest["role"] == "assistant"
        assert newest["text"] == "Done."
        assert second["index"] == 2
        assert second["role"] == "tool"  # tool-only step
        assert second["text"] is None
        assert second["tool_calls"] == 1

    def test_older_page_via_cursor_reaches_top(self, client: TestClient) -> None:
        body = client.get(
            "/sessions/sess-1/messages", params={"limit": 2, "before": 2}
        ).json()
        assert body["has_more"] is False
        assert body["next_before"] is None
        older, oldest = body["messages"]
        assert older["index"] == 1
        assert older["role"] == "assistant"
        assert older["text"] == "Sure, looking now."
        assert older["tool_calls"] == 1  # tool_use folded onto the text turn
        assert oldest["index"] == 0
        assert oldest["role"] == "user"
        assert oldest["text"].startswith("peek AnotherAgent")

    def test_messages_unknown_id_404(self, client: TestClient) -> None:
        assert client.get("/sessions/nope/messages").status_code == 404

    def test_tool_steps_carry_detail_labels(self, tmp_path: Path) -> None:
        # A tool-only assistant step should expose per-tool detail (name,
        # human label, diff stat) — the mobile analogue of the terminal's
        # "Update(x.py) +2 -1" line, not a bare count.
        events = [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"description": "list files"}},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/Users/dev/proj/config.py",
                                "old_string": "a\nb",
                                "new_string": "a\nb\nc",
                            },
                        },
                    ],
                },
            },
        ]
        _write_session(tmp_path, "-Users-dev-proj", "tools-1", events)
        idx = SessionIndex(projects_dir=tmp_path)
        page = idx.get_messages("tools-1")
        tool_turn = next(m for m in page["messages"] if m["role"] == "tool")
        assert tool_turn["tool_calls"] == 2
        labels = [t["label"] for t in tool_turn["tools"]]
        assert labels == ["Bash: list files", "Update(config.py)"]
        edit = tool_turn["tools"][1]
        assert edit["stat"] == "+3 -2"  # 3 new lines, 2 old lines

    def test_injected_notifications_filtered_but_mobile_kept(self, tmp_path: Path) -> None:
        # Background task-notifications / system-reminders arrive as user
        # turns and must be dropped — but they share promptSource "sdk"
        # with real mobile messages, so the filter is content-based. A
        # mobile turn (plain text, sdk source) MUST survive.
        events = [
            {"type": "user", "cwd": CWD, "uuid": "u1", "promptSource": "typed", "message": {"role": "user", "content": "real question"}},
            {"type": "user", "uuid": "n1", "promptSource": "system", "message": {"role": "user", "content": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}},
            {"type": "user", "uuid": "s1", "promptSource": "system", "message": {"role": "user", "content": "<system-reminder>be nice</system-reminder>"}},
            {"type": "user", "uuid": "m1", "promptSource": "sdk", "message": {"role": "user", "content": "hello from mobile"}},
            {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        ]
        _write_session(tmp_path, "-Users-dev-proj", "notif-1", events)
        idx = SessionIndex(projects_dir=tmp_path)
        user_texts = [m["text"] for m in idx.get_messages("notif-1")["messages"] if m["role"] == "user"]
        assert "real question" in user_texts
        assert "hello from mobile" in user_texts  # sdk-sourced mobile msg survives
        assert all("task-notification" not in t and "system-reminder" not in t for t in user_texts)

    def test_ismeta_image_descriptors_are_not_turns(self, tmp_path: Path) -> None:
        # Pasted/read screenshots inject an isMeta user event whose text
        # is "[Image: … Multiply coordinates …]". It is metadata, not a
        # human turn, so it must appear in neither the chat nor the card
        # count (regression: these leaked in as bogus "you" turns).
        events = [
            {
                "type": "user",
                "cwd": CWD,
                "uuid": "u1",
                "promptSource": "typed",
                "message": {"role": "user", "content": "real question"},
            },
            {
                "type": "user",
                "isMeta": True,
                "uuid": "m1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[Image: original 1179x2556, Multiply coordinates…]"}
                    ],
                },
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            },
        ]
        _write_session(tmp_path, "-Users-dev-proj", "meta-1", events)
        idx = SessionIndex(projects_dir=tmp_path)

        page = idx.get_messages("meta-1")
        assert page["total"] == 2  # the typed turn + the assistant turn only
        assert all("Multiply coordinates" not in (m["text"] or "") for m in page["messages"])
        # and the card count excludes the meta event too
        card = idx.get_card("meta-1")
        assert card["message_count"] == 2


# ── Cache behaviour ───────────────────────────────────────────────────


class TestResume:
    def _mock_ok(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
        from services.claude_runner import ClaudeResult

        def fake_run_blocking(args, cwd, timeout_seconds, job_id):
            assert "--resume" in args and "sess-1" in args  # continues the session
            assert cwd == CWD  # spawned in the session's original dir
            return ClaudeResult(
                returncode=0,
                stdout=json.dumps({"result": reply, "session_id": "sess-1"}),
                stderr="",
                timed_out=False,
                cancelled=False,
                duration_seconds=0.1,
            )

        monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", fake_run_blocking)

    def test_resume_returns_reply(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_ok(monkeypatch, "hi there")
        r = client.post("/sessions/sess-1/resume", json={"message": "hello"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "session_id": "sess-1", "reply": "hi there"}

    def test_resume_unknown_session_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_ok(monkeypatch, "unused")
        assert client.post("/sessions/nope/resume", json={"message": "hi"}).status_code == 404

    def test_resume_empty_message_rejected(self, client: TestClient) -> None:
        # pydantic min_length rejects "" outright (422); no spawn happens.
        assert client.post("/sessions/sess-1/resume", json={"message": ""}).status_code == 422

    def test_latest_model_reads_session_model(self, tmp_path: Path) -> None:
        _write_session(
            tmp_path,
            "-Users-dev-proj",
            "modelled",
            [
                {"type": "user", "cwd": CWD, "uuid": "u1", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "model": "claude-sonnet-5", "content": [{"type": "text", "text": "yo"}]}},
            ],
        )
        idx = SessionIndex(projects_dir=tmp_path)
        assert idx.latest_model("modelled") == "claude-sonnet-5"

    def test_resume_falls_back_to_opus_when_no_model(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.claude_runner import ClaudeResult

        captured: dict = {}

        def capture(args, cwd, timeout_seconds, job_id):
            captured["args"] = args
            return ClaudeResult(
                returncode=0, stdout=json.dumps({"result": "ok"}), stderr="",
                timed_out=False, cancelled=False, duration_seconds=0.1,
            )

        monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", capture)
        # fixture session sess-1 has no message.model → Opus 4.8 fallback
        r = client.post("/sessions/sess-1/resume", json={"message": "hi"})
        assert r.status_code == 200
        assert "claude-opus-4-8" in captured["args"]

    def test_resume_stream_emits_sse_events(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        # Fake claude stdout: two text deltas + an assistant msg with a
        # tool_use, so we can assert text/tool/done SSE frames.
        lines = [
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"description": "list files"}}]}}),
            json.dumps({"type": "result"}),
        ]

        class FakeProc:
            stdout = [f"{l}\n" for l in lines]

        @contextlib.contextmanager
        def fake_stream(args, cwd, job_id):
            assert "--include-partial-messages" in args and "--resume" in args
            yield FakeProc()

        monkeypatch.setattr(sessions_router.claude_runner, "streaming_subprocess", fake_stream)

        with client.stream("POST", "/sessions/sess-1/resume/stream", json={"message": "hi"}) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            body = "".join(r.iter_text())

        assert "event: text" in body
        assert '"chunk": "Hel"' in body and '"chunk": "lo"' in body
        assert "event: tool" in body and "Bash: list files" in body
        assert "event: done" in body

    def test_resume_stream_unknown_session_404(self, client: TestClient) -> None:
        assert client.post("/sessions/nope/resume/stream", json={"message": "hi"}).status_code == 404

    def test_resume_spawn_error_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.claude_runner import ClaudeResult

        def boom(args, cwd, timeout_seconds, job_id):
            return ClaudeResult(
                returncode=-1, stdout="", stderr="", timed_out=False,
                cancelled=False, duration_seconds=0.0, spawn_error="claude not found",
            )

        monkeypatch.setattr(sessions_router.claude_runner, "run_blocking", boom)
        assert client.post("/sessions/sess-1/resume", json={"message": "hi"}).status_code == 500


class TestCaching:
    def test_turns_reparse_only_when_file_changes(
        self, index: SessionIndex, tmp_path: Path
    ) -> None:
        # First read populates the mtime-keyed turn cache.
        assert index.get_messages("sess-1")["total"] == 4
        path = tmp_path / "-Users-dev-proj" / "sess-1.jsonl"
        key = str(path)
        assert key in index._turns_cache

        # Appending a turn changes mtime+size → cache invalidates → new
        # turn shows up (mirrors a live session growing).
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u3",
                        "message": {"role": "user", "content": "one more"},
                    }
                )
                + "\n"
            )
        assert index.get_messages("sess-1")["total"] == 5
