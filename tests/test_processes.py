"""The Processes view (ticket 29): Claude processes on the Mac with the
Bash commands under them, secrets hidden, and stopping only such a
command."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import processes as processes_router
from services import process_view
from services.process_view import Proc

WRAP = (
    "/bin/zsh -c source /Users/ken/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true && "
    "eval '{cmd}' < /dev/null && pwd -P >| /tmp/claude-ab-cwd"
)

PS = "\n".join(
    [
        "    1     0     1  10-00:00:00  0.0 /sbin/launchd",
        "  900     1   900  02:00:00  0.1 python uvicorn main:app",  # the bridge
        " 4698   900  4698     42:18  0.2 claude -p fix the events page",  # a phone turn
        "10297  4698 10297     29:53  0.0 " + WRAP.format(cmd="cd ~/theme && shopify theme dev --store=kodama.myshopify.com"),
        "10305 10297 10297     29:53  4.4 node /usr/bin/shopify theme dev",
        " 7225     1  7225  01:02:03  1.0 claude --dangerously-skip-permissions",  # a Mac tab
        "11000  7225 11000     00:05  9.0 " + WRAP.format(cmd="curl -H '\"'\"'X-Coder-Key: abc123'\"'\"' http://x"),
        "11500  7225 11500     00:03  0.0 node mcp-server.js",  # not a Bash command
        "12000     1 12000     00:10  0.0 vim notes.txt",  # not Claude's
        "garbage",
    ]
)

RECORDS = [
    {"pid": 4698, "sessionId": "phone-sess", "entrypoint": "sdk-cli", "status": "busy"},
    {"pid": 7225, "sessionId": "tab-sess", "entrypoint": "cli", "status": "idle"},
    {"pid": 5555, "sessionId": "dead", "entrypoint": "cli"},  # process gone
]


class TestParsing:
    @pytest.mark.parametrize(
        "text,seconds", [("00:05", 5), ("42:18", 2538), ("01:02:03", 3723), ("2-03:00:00", 183600), ("x", 0)]
    )
    def test_elapsed(self, text, seconds) -> None:
        assert process_view.parse_elapsed(text) == seconds

    def test_bash_command_is_taken_out_of_the_wrapper(self) -> None:
        cmd = WRAP.format(cmd="echo '\"'\"'hi'\"'\"' && ls")
        assert process_view.bash_command(cmd) == "echo 'hi' && ls"
        assert process_view.bash_command("node mcp-server.js") is None

    def test_a_heredoc_command_has_no_dev_null_and_escaped_newlines(self) -> None:
        cmd = (
            "/bin/zsh -c source /Users/ken/.claude/shell-snapshots/s.sh && "
            "eval 'python3 - <<EOF\\012print(1)\\012EOF' && pwd -P >| /tmp/claude-1-cwd"
        )
        assert process_view.bash_command(cmd) == "python3 - <<EOF\nprint(1)\nEOF"

    @pytest.mark.parametrize(
        "raw,hidden",
        [
            ("curl -H 'X-Coder-Key: abc123' http://x", "X-Coder-Key: ***"),
            ("curl -H 'Authorization: Bearer tok.en'", "Bearer ***"),
            ("STRIPE=sk_live_8b727a1b4dd7 run", "sk_live_***"),
            ("OPENAI_API_KEY=sk-abcdefghijklmnopqrstu node x", "OPENAI_API_KEY=***"),
            ("PASSWORD=hunter2 ./deploy", "PASSWORD=***"),
        ],
    )
    def test_secrets_are_redacted(self, raw, hidden) -> None:
        out = process_view.redact(raw)
        assert hidden in out
        for secret in ("abc123", "tok.en", "8b727a1b4dd7", "abcdefghijklmnopqrstu", "hunter2"):
            assert secret not in out


class TestSnapshot:
    def test_lists_claude_processes_with_their_commands(self) -> None:
        entries = process_view.snapshot(RECORDS, process_view.parse_ps(PS), bridge_pid=900)
        assert [(e["pid"], e["origin"]) for e in entries] == [(4698, "phone"), (7225, "mac_tab")]
        phone = entries[0]
        assert phone["session_id"] == "phone-sess" and phone["status"] == "busy"
        assert phone["commands"] == [
            {
                "pid": 10297,
                "command": "cd ~/theme && shopify theme dev --store=kodama.myshopify.com",
                "elapsed": 1793,
                "cpu": 4.4,
                "processes": 2,
            }
        ]
        tab = entries[1]
        assert [c["pid"] for c in tab["commands"]] == [11000]  # the MCP server is not a command
        assert "abc123" not in tab["commands"][0]["command"]

    def test_only_bash_commands_of_claude_are_stoppable(self) -> None:
        rows = process_view.parse_ps(PS)
        assert process_view.stoppable(10297, RECORDS, rows).pgid == 10297
        for pid in (4698, 7225, 900, 10305, 11500, 12000, 99999):
            assert process_view.stoppable(pid, RECORDS, rows) is None


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(processes_router, "_records", lambda: RECORDS)
    monkeypatch.setattr(processes_router.process_view, "read_ps", lambda: process_view.parse_ps(PS))
    monkeypatch.setattr(processes_router.os, "getpid", lambda: 900)
    monkeypatch.setattr(processes_router.session_index, "get_card", lambda sid: {"title": f"Title {sid}"})
    app = FastAPI()
    app.include_router(processes_router.router)
    return TestClient(app)


class TestRoutes:
    def test_list(self, client) -> None:
        body = client.get("/processes").json()
        assert body["commands"] == 2
        assert body["claude"][0]["title"] == "Title phone-sess"

    def test_stop_ends_the_command_group(self, client, monkeypatch) -> None:
        ended = []
        monkeypatch.setattr(processes_router, "_end_group", lambda pgid, pid: ended.append((pgid, pid)))
        r = client.post("/processes/10297/stop", json={"confirm": 10297})
        assert r.status_code == 200 and ended == [(10297, 10297)]

    def test_stop_needs_the_pid_twice(self, client, monkeypatch) -> None:
        monkeypatch.setattr(processes_router, "_end_group", lambda pgid, pid: pytest.fail("must not stop"))
        assert client.post("/processes/10297/stop", json={"confirm": 1}).status_code == 422

    @pytest.mark.parametrize("pid", [4698, 900, 12000, 99999])
    def test_stop_refuses_anything_but_a_command(self, client, monkeypatch, pid) -> None:
        monkeypatch.setattr(processes_router, "_end_group", lambda pgid, p: pytest.fail("must not stop"))
        monkeypatch.setattr(processes_router.os, "kill", lambda *a: pytest.fail("must not signal"))
        assert client.post(f"/processes/{pid}/stop", json={"confirm": pid}).status_code == 404
