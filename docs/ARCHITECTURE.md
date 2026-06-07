# Architecture

This document covers what `another_bridge` actually does at runtime: the
two main flows (claude_code engine bridge + webhook tools), the
polling-reconnect path, the persistence model, the lifecycle of the
TTL reaper, and the security model.

The bird's-eye view:

```mermaid
graph TB
    subgraph User
        Phone[📱 Phone / Browser]
    end

    subgraph Platform[AnotherAgent Platform]
        EM[Engineering Manager Agent]
        Planner[Planner Agent]
        Dev[Developer Agent]
        Bridge[Bridge dispatcher<br/><i>generateResponse/bridge.ts</i>]
        WebhookDispatch[Webhook dispatcher<br/><i>executeTool.dispatch.ts</i>]
    end

    subgraph Tunnel
        Ngrok[ngrok HTTPS tunnel]
    end

    subgraph Coder[another_bridge]
        ChatStream[POST /chat/stream<br/>SSE]
        Webhooks["POST /tools/instruct_*<br/>POST /tools/github_*<br/>POST /tools/list_repos"]
        Jobs[GET /jobs/&lt;id&gt;/status<br/>GET /jobs/&lt;id&gt;/chat/status<br/>POST /jobs/&lt;id&gt;/cancel]
        Auth[GET /auth/verify]
        Health[GET /health]

        ClaudeRunner[claude_runner<br/><i>subprocess wrapper</i>]
        SessionDB[(SQLite<br/>session_store)]
        JobMgr[JobManager<br/><i>in-memory</i>]
        Reaper[Reaper daemon<br/><i>TTL prune</i>]
    end

    GitHub[GitHub REST API]
    Claude[claude CLI subprocess<br/>--dangerously-skip-permissions]
    Repo[(Local repo clone)]

    Phone --> EM
    EM --> Planner
    EM --> Dev
    EM <-->|claude_code engine| Bridge
    Planner & Dev --> WebhookDispatch
    Bridge -->|SSE + auth| Ngrok
    WebhookDispatch -->|HTTP + auth| Ngrok
    Ngrok --> ChatStream
    Ngrok --> Webhooks
    Ngrok --> Jobs
    Ngrok --> Auth
    Ngrok --> Health

    ChatStream --> ClaudeRunner
    Webhooks --> ClaudeRunner
    Webhooks --> GitHub
    ClaudeRunner --> Claude
    Claude --> Repo
    ChatStream --> SessionDB
    ChatStream --> JobMgr
    Webhooks --> JobMgr
    Jobs --> JobMgr
    Reaper -->|prune| JobMgr
    Reaper -->|prune| SessionDB
```

---

## 1. Bridge: `claude_code` engine via `/chat/stream`

The platform's `claude_code`-engine agents short-circuit their LLM
pipeline. Instead of invoking GPT/Claude through the platform's
provider, they POST to `/chat/stream` and stream the response back
to the user as if it had come from any other model.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Frontend as Frontend (web/mobile)
    participant Backend as Platform Backend
    participant Bridge as another_bridge /chat/stream
    participant Store as session_store (SQLite)
    participant Mgr as JobManager
    participant Claude as claude CLI

    User->>Frontend: send message
    Frontend->>Backend: POST /chat/stream
    Backend->>Backend: agent.llmEngine == "claude_code"
    Backend->>Bridge: POST /chat/stream<br/>X-Coder-Key + body
    Bridge->>Bridge: validate_repo_path(WORKSPACE_ROOT)
    Bridge->>Store: get_session(conversation_id)
    Note over Bridge,Store: --resume <session_id> if found,<br/>else fresh session
    Bridge->>Mgr: create job (kind=chat_stream)
    Bridge-->>Backend: SSE: kickoff(jobId, cancelUrl, statusUrl)
    Backend->>Frontend: relay kickoff for cancel + reconnect

    Bridge->>Claude: spawn (cwd = repo_path)
    loop streaming
        Claude-->>Bridge: stream-json events
        Bridge->>Mgr: append_text(chunk)
        Bridge-->>Backend: SSE: text(chunk)
        Backend-->>Frontend: SSE: response_token
    end
    Claude-->>Bridge: exit
    alt clean exit
        Bridge->>Store: set_session(conversation_id, captured_id)
        Bridge-->>Backend: SSE: done(sessionId)
        Bridge->>Mgr: mark_done
    else cancelled / error
        Bridge-->>Backend: SSE: error(reason)
        Bridge->>Mgr: mark_failed(kind=cancelled/claude_failed/spawn_failed)
        Note over Bridge,Store: failed runs do NOT persist session_id<br/>so a retry starts clean
    end
```

**Key contracts:**
- Kickoff event is the FIRST SSE message, before any text. It carries `jobId`, `cancelUrl`, `statusUrl`, `pollEverySeconds`, `pollMaxSeconds` so the platform can register the cancel and polling primitives upfront.
- `set_session` runs **only** after a clean run. Failed/cancelled runs leave the prior `session_id` intact so the user's NEXT attempt can still resume the working state.
- `accumulated_text` is mirrored into the JobManager job alongside the SSE stream so the polling-reconnect path (next section) can read mid-flight progress.

### Mobile reconnect (SSE drop)

iOS Safari aggressively suspends background tabs. When a phone locks
mid-Claude-Code-response, the SSE socket dies and the platform's
frontend transitions to a polling reconnect:

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Frontend
    participant Backend
    participant BridgeStatus as backend /conversations/:id/bridge-status
    participant Coder as another_bridge /jobs/:id/chat/status

    Note over User,Frontend: 📱 lock phone — SSE dies
    User->>Frontend: 📱 unlock
    Frontend->>Frontend: visibilitychange listener
    Note over Frontend: Reconnecting… amber pill,<br/>NOT a red error
    Frontend->>Backend: GET /conversations/:id/bridge-status
    Backend->>Coder: GET /jobs/:jobId/chat/status<br/>(forwards X-Coder-Key)
    Coder-->>Backend: {status, accumulatedText, done}
    Backend-->>Frontend: same shape
    alt status == running
        Frontend->>Frontend: update bubble with accumulatedText
        Frontend->>Backend: poll again in pollEverySeconds
    else done == true
        Frontend->>Backend: GET /messages?since= (final saved msg)
        Note over Frontend: stop polling, render final
    end
```

The accumulator + polling endpoints are what make this seamless — the
user sees the partial text appear within a few seconds of unlocking,
instead of staring at a blank bubble until the graph-side `saveExchange`
lands and the next regular `/messages` poll picks it up.

---

## 2. Webhook tools: `/tools/instruct_planning` + `/tools/instruct_implementation`

The planning + implementation flow is
async-webhook. The platform fires a kickoff POST, gets a `job_id`
back immediately, then polls `/jobs/<id>/status` until done.

```mermaid
sequenceDiagram
    autonumber
    participant Platform as Platform executeTool dispatcher
    participant Coder as another_bridge /tools/instruct_implementation
    participant Schema as ImplementationRequest<br/>(pydantic)
    participant Repo as Local repo (validated cwd)
    participant Claude as claude CLI
    participant GitHub as GitHub REST API

    Platform->>Coder: POST /tools/instruct_implementation<br/>{arguments: {ticket_number, ticket_body, plan, ...}}
    Coder->>Schema: model_validate(args)
    alt invalid
        Schema-->>Coder: ValidationError
        Coder-->>Platform: 200 {error: "<friendly hint>"}
        Note over Coder,Platform: 200 + body so the platform's<br/>response.ok check surfaces the<br/>hint to the LLM (self-correction)
    end
    Coder->>Coder: validate_repo_path(WORKSPACE_ROOT)
    Coder->>Coder: create JobManager job
    Coder-->>Platform: 202 {job_id, status_url, cancel_url}
    Platform->>Platform: register polling loop

    par background
        Coder->>Repo: git status — assert clean
        Coder->>Repo: git checkout & pull base
        Coder->>Repo: git checkout -b agent/ticket-N
        Coder->>Claude: spawn with implement_prompt
        Claude->>Repo: edit + commit per plan step
        Coder->>Repo: git push origin
        Coder->>GitHub: POST /repos/:slug/pulls
        GitHub-->>Coder: PR opened
        Coder->>Coder: mark_done({pr_url, branch_name, commits, ...})
    and platform polling
        loop every pollEverySeconds
            Platform->>Coder: GET /jobs/:id/status
            Coder-->>Platform: {status: running} or {status: done, result: {...}}
        end
    end
```

The same shape applies to `/tools/instruct_planning` minus the
git-branch-and-PR steps — it just runs Claude with the planning
prompt and returns the plan in `result.plan` plus an optional
`__summary__` and `__context__` for the platform's artifact system.

**Validation hint contract:** the platform's webhook dispatcher uses
`response.ok`. A `422 Unprocessable Entity` from FastAPI's default
handler would degrade to "webhook returned 422" in the LLM trace —
losing the recovery hint. So the schemas catch `ValidationError`
inside the route handler and return `200 OK` with `{"error":
"<actionable hint>"}` instead. The Planner Agent reads the hint
and self-corrects on its next pass.

**Failure surfacing:** every `mark_failed` call now includes a
structured `kind` (see `services/errors.py`):
- `spawn_failed` — `claude` binary missing.
- `timeout` — Claude exceeded the per-request `timeout_seconds`.
- `cancelled` — operator hit cancel.
- `claude_failed` — non-zero exit, stderr in `error`.
- `git_push_failed` — push rejected (auth, branch protection, network).
- `pr_create_failed` — GitHub PR API failure (422 enriched with diagnose+fix steps).

---

## 3. Persistence: SQLite session store + JobManager

Two layers, by design:

```mermaid
graph LR
    subgraph Process[uvicorn process]
        JM[JobManager._jobs<br/><i>dict: job_id → Job</i>]
    end

    subgraph Disk
        DB[(sessions.db<br/>conversation_id<br/>session_id<br/>last_seen_at)]
    end

    JM -->|in-memory only<br/>lost on restart<br/>but reaper prunes| JM
    DB -->|persists across restarts<br/>reaper prunes inactive >7d| DB
```

**Why split:**
- A `Job` is short-lived (minutes). Persisting it would buy us nothing
  — the platform polls actively for as long as the job runs, and
  once it's done the result has already flowed back. Dead jobs are
  pruned by the reaper.
- A `(conversation_id → session_id)` mapping is long-lived. The user
  expects to resume yesterday's chat into the same Claude Code
  session today, even after a `uvicorn` restart. SQLite is just
  enough — one row per conversation, two short strings, one
  timestamp.

**Schema:**
```sql
CREATE TABLE sessions (
    conversation_id TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    last_seen_at    INTEGER NOT NULL  -- unix epoch seconds
);
CREATE INDEX sessions_last_seen_at_idx ON sessions(last_seen_at);
```

WAL mode is enabled for crash safety. Single connection +
`threading.Lock` for serialized access (FastAPI's worker pool can hit
the store concurrently; SQLite connections aren't thread-safe by
default).

---

## 4. TTL reaper

A daemon thread spun up at app startup (FastAPI lifespan) and joined
on shutdown:

```mermaid
stateDiagram-v2
    [*] --> Started: lifespan startup
    Started --> Pruning: initial prune\n(immediate)
    Pruning --> Sleeping: ANOTHER_CODER_REAPER_INTERVAL_SECONDS
    Sleeping --> Pruning: timer fires
    Pruning --> Sleeping: prune_once()
    Pruning --> Stopped: stop_event.set()
    Sleeping --> Stopped: stop_event.set()
    Stopped --> [*]: thread.join()
```

`prune_once()` does both prunes synchronously:
- `JobManager.prune_finished_older_than(ANOTHER_CODER_JOB_TTL_SECONDS)` — drops `done`/`failed` jobs whose `finished_at` is past the cutoff. **Running jobs are never touched** (cancel + status routes need them).
- `SessionStore.prune_older_than(ANOTHER_CODER_SESSION_TTL_SECONDS)` — drops session rows inactive past the cutoff.

Errors from either prune are logged and swallowed so a transient DB
lock or job-dict contention can't take the reaper down for the rest
of the process lifetime. The thread is daemonized — process exit
reaps it even if `stop()` somehow blocks.

---

## 5. Security model

Three layers:

```mermaid
graph TD
    Req[HTTP request] --> Auth{verify_api_key<br/>X-Coder-Key matches?}
    Auth -->|no| R401[401 Unauthorized]
    Auth -->|env var unset| R503[503 misconfig]
    Auth -->|yes| Limit{slowapi<br/>under bucket cap?}
    Limit -->|over| R429[429 rate limit]
    Limit -->|ok| Path{path validated?<br/>chat/instruct only}
    Path -->|outside WORKSPACE_ROOT<br/>or symlink escape| R400[400 Bad Request]
    Path -->|in-bounds| Spawn[spawn claude<br/>cwd = realpath]
```

**Layer 1 — auth.** Every route except `/health` runs through
`Depends(verify_api_key)`. `hmac.compare_digest` against
`ANOTHER_CODER_API_KEY` (constant-time). Empty env var returns 503
rather than allow-all — refuses to silently misconfig.

**Layer 2 — rate limiting.** `slowapi` keyed on hashed `X-Coder-Key`
(IP fallback for `/health`, but `/health` doesn't run the limiter).
Conservative defaults sized for a single-operator deployment;
tunable per surface via env. Bounds the leaked-key blast radius
before rotation.

**Layer 3 — path bounds.** Every Claude Code spawn includes
`--dangerously-skip-permissions`, so the spawn's `cwd` is the actual
security boundary. `validate_repo_path` resolves the path via
`os.path.realpath` (collapses `..`, follows symlinks) and refuses
anything not inside `WORKSPACE_ROOT`. Applied identically on
`/chat/stream`, `/tools/instruct_planning`, and
`/tools/instruct_implementation` — three pathways, one gate. When
`WORKSPACE_ROOT` is unset, falls back to a bare `isdir` check for
backward-compatibility — but that's strictly weaker, so production
deployments should always set it.

**What's intentionally NOT here:**
- **CORS middleware.** This is a server-to-server bridge consumed by
  the platform's backend, not a browser. A CORS policy would mislead
  about who's actually calling these routes.
- **Per-tool keys.** All routes share `ANOTHER_CODER_API_KEY`. The
  granularity buys little for a single-operator deployment;
  deferred until a real multi-user use case appears.

---

## 6. Module map

| Module | Responsibility |
|---|---|
| `main.py` | App construction, lifespan (reaper start/stop), middleware wiring, router registration. |
| `auth.py` | `verify_api_key` FastAPI Depends — header check + constant-time compare. |
| `config.py` | `Settings(BaseSettings)` from pydantic-settings + back-compat module-level constants. |
| `jobs.py` | `JobManager` + `Job` dataclass — in-memory job state, cancel + structured failure kinds. |
| `routers/auth.py` | `/auth/verify` probe endpoint. |
| `routers/chat.py` | `/chat/stream` SSE bridge to Claude Code. Repo-path validation, session resume, accumulator mirroring. |
| `routers/health.py` | `/health` (open). |
| `routers/jobs.py` | `/jobs/<id>/status`, `/jobs/<id>/chat/status`, `/jobs/<id>/cancel`. |
| `routers/repos.py` | `/tools/list_repos` + `validate_repo_path` (the workspace gate). |
| `routers/github.py` | `/tools/github_search_issues`, `/tools/github_get_issue`. |
| `routers/planning.py` | `/tools/instruct_planning` — webhook + background runner. |
| `routers/implementation.py` | `/tools/instruct_implementation` — webhook + background runner + git/GitHub orchestration. |
| `routers/_schemas.py` | `PlanningRequest`, `ImplementationRequest` pydantic models with the LLM-recovery hint contract. |
| `services/claude_runner.py` | Subprocess lifecycle for `claude` (run_blocking + streaming_subprocess). Reads `CLAUDE_BIN_PATH`. |
| `services/git_service.py` | `_run_git`, branch existence checks, default-branch detection, base-branch resolution, owner/repo regex. |
| `services/github_service.py` | GitHub REST API client (search_issues, get_issue, create_pr) + `format_pr_creation_error`. |
| `services/repo_context.py` | `build_selected_repo_context` for the `__context__` artifact emission. |
| `services/request.py` | `extract_args` envelope unwrap (`{arguments: {...}}` → `{...}`). |
| `services/errors.py` | Failure-kind constants used by `mark_failed` + status responses. |
| `services/session_store.py` | SQLite-backed `(conversation_id → session_id)` map. |
| `services/reaper.py` | Daemon thread that prunes JobManager + session store on a timer. |
| `services/rate_limiter.py` | slowapi `Limiter` instance + `_coder_key_or_remote` key function. |

---

## 7. Where to read next

- [`README.md`](../README.md) — setup + operator's how-to.
- [`docs/SECURITY.md`](SECURITY.md) — threat model, recommended deployment posture, key-rotation procedure. The "should I install this on my main laptop" doc.
- [`docs/CURL_EXAMPLES.md`](CURL_EXAMPLES.md) — raw cURL probes for every route.
