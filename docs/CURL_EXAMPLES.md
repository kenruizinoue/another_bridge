# cURL Examples

Raw cURL probes for every public route on the bridge. Useful for
smoke-testing a deploy, debugging a stuck integration, or
hand-driving the API without spinning up the AnotherAgent platform.

Set these once per shell session:

```bash
export CODER_URL=http://127.0.0.1:8000          # or your ngrok URL
export CODER_KEY=<your ANOTHER_CODER_API_KEY>
```

---

## Health (no auth)

```bash
curl "$CODER_URL/health"
```

```json
{
  "ok": true,
  "service": "another_bridge",
  "version": "0.1.0",
  "claude_probe": {"ok": true, "detail": "claude-code 1.x.y"},
  "session_store_reachable": true
}
```

`claude_probe.ok` flips to `false` (with the failure detail) if the
boot-time `claude --version` probe couldn't invoke the binary —
useful for triaging NVM PATH issues without tailing logs.

---

## Auth probe

```bash
curl -H "X-Coder-Key: $CODER_KEY" "$CODER_URL/auth/verify"
```

- `200 {"ok": true}` — key matches.
- `401` — key wrong.
- `503` — bridge has no `ANOTHER_CODER_API_KEY` configured.

This is what the AnotherAgent **Settings → Integrations → another_coder
→ Connect** dialog calls before saving.

---

## Chat stream (SSE)

```bash
curl -N -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"conversation_id":"demo","message":"hello, list the files in this repo","repo_path":"/Users/you/Projects/your-repo"}' \
     "$CODER_URL/chat/stream"
```

- `-N` disables curl's output buffering so SSE chunks flush as they arrive.
- The first event is always `kickoff` carrying `jobId`, `cancelUrl`, `statusUrl`, `pollEverySeconds`, `pollMaxSeconds`.
- Subsequent `text` events stream Claude Code's response.
- A final `done` (success) or `error` event ends the stream.

Same `conversation_id` on the next call resumes the same Claude Code
session via `--resume <session_id>` (the mapping survives `uvicorn`
restarts via the SQLite session store). Failed runs deliberately
don't persist the session_id, so a retry starts clean.

---

## Webhook tools

All four tools accept either a flat body or `{"arguments": {...}}`
(the platform's webhook envelope). Examples below use the flat form.

### Plan a ticket — `POST /tools/instruct_planning`

Async — returns immediately with a `job_id`; poll `/jobs/<id>/status`.

```bash
curl -X POST \
     -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"ticket_number":41,"ticket_body":"Add dark mode toggle","repo_path":"/Users/you/Projects/your-repo"}' \
     "$CODER_URL/tools/instruct_planning"
```

Optional fields: `timeout_seconds` (capped at 1800).

### Implement a plan — `POST /tools/instruct_implementation`

Async; opens a real branch + PR on success.

```bash
curl -X POST \
     -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"ticket_number":41,"ticket_body":"Add dark mode toggle","plan":"<paste plan from /tools/instruct_planning>","repo_path":"/Users/you/Projects/your-repo"}' \
     "$CODER_URL/tools/instruct_implementation"
```

Optional fields: `base_branch` (defaults to the remote's default branch), `timeout_seconds`.

### List workspace repos — `POST /tools/list_repos`

Synchronous. Walks one level under `WORKSPACE_ROOT`, returns repos with `.git/` + an origin remote.

```bash
curl -X POST \
     -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{}' \
     "$CODER_URL/tools/list_repos"
```

### GitHub search / get — `POST /tools/github_search_issues` + `POST /tools/github_get_issue`

Synchronous. Use `GITHUB_DEFAULT_REPO` from `.env` when `repo` is omitted.

```bash
curl -X POST \
     -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"label":"bug","state":"open"}' \
     "$CODER_URL/tools/github_search_issues"

curl -X POST \
     -H "X-Coder-Key: $CODER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"issue_number":41}' \
     "$CODER_URL/tools/github_get_issue"
```

---

## Job polling

### Status

```bash
curl -H "X-Coder-Key: $CODER_KEY" "$CODER_URL/jobs/<job_id>/status"
```

While running:
```json
{"job_id":"...","kind":"instruct_planning","status":"running","elapsed_seconds":12.4}
```

On clean completion:
```json
{"job_id":"...","status":"done","result":{...}}
```

On failure, `error` plus a structured `error_kind` (one of
`spawn_failed`, `timeout`, `cancelled`, `claude_failed`,
`git_push_failed`, `pr_create_failed`):
```json
{"job_id":"...","status":"failed","error":"...","error_kind":"claude_failed"}
```

### Chat status (mid-flight accumulator)

The polling-reconnect path the platform uses when SSE drops mid-stream (e.g. a phone locks).

```bash
curl -H "X-Coder-Key: $CODER_KEY" "$CODER_URL/jobs/<job_id>/chat/status"
```

Returns `{status, accumulatedText, done}` — the partial text generated so far so the UI can re-render the in-progress bubble without restarting the spawn.

### Cancel

```bash
curl -X POST -H "X-Coder-Key: $CODER_KEY" "$CODER_URL/jobs/<job_id>/cancel"
```

Sends SIGTERM to the Claude subprocess (and process group), with a 3s grace period before SIGKILL. The next `/jobs/<id>/status` will report `error_kind: "cancelled"`.

---

## Rate-limit response

When a per-route bucket is exhausted (`slowapi`):

```
HTTP/1.1 429 Too Many Requests
{"error":"rate limited","detail":"30 per 1 minute"}
```

Tunable via `ANOTHER_CODER_RATE_LIMIT_*` env vars — see [`README.md`](../README.md).
