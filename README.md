# another_bridge

> ⚠️ **Beta — use at your own risk.** Runs `claude --dangerously-skip-permissions` against a directory you choose. Set `WORKSPACE_ROOT` to a path you'd be OK losing, and never expose the bridge's API key in screenshots, recordings, or LLM prompts. Threat model: [`docs/SECURITY.md`](docs/SECURITY.md).

FastAPI bridge that lets the AnotherAgent platform run **Claude Code** on your machine. Two surfaces:

1. **`POST /chat/stream`** — the **claude_code engine bridge**. AnotherAgent's `claude_code` agents short-circuit their LLM pipeline and stream chat through here, so you can talk to Claude Code itself (your subscription, not platform credits) from voice / mobile / dashboard.
2. **`POST /tools/instruct_planning`** + **`/tools/instruct_implementation`** — webhook tools the planning + implementation agents call. Plans tickets against your real repo, implements plans into real PRs.

When wired up:

> *"Plan ticket #41"* → plan returned, grounded in real files
> *"Go ahead, implement it"* → branch + commits + PR open on GitHub

---

## Prerequisites

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/quickstart)** — `npm install -g @anthropic-ai/claude-code`. Confirm `claude --version` works.
- **[ngrok](https://ngrok.com/download)** — free account is enough.
- **A GitHub fine-grained PAT** with these permissions on the target repo:
  - `Issues: Read`
  - `Pull requests: Read and write`
  - `Contents: Read and write`
- **Local clone(s) of your target repo(s)** — at minimum the one you want to demo on.
- **An AnotherAgent account** — sign up at <https://anotheragent.dev>. The frontend + backend are hosted; you don't deploy those yourself. The only thing that runs locally is `another_bridge` (this repo), because Claude Code itself runs on your machine with your subscription.

---

## Setup

### 1. Clone + install

```bash
git clone <this-repo-url> another_bridge
cd another_bridge
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

Project metadata (deps, version, etc.) lives in [`pyproject.toml`](pyproject.toml). For dev/test work that includes `pytest` + `pytest-cov`, use `pip install -e ".[dev]"` instead.

### 2. Configure `.env`

```bash
cp .env.example .env
```

The minimum you must set:

```dotenv
GITHUB_PAT=github_pat_xxxxxxxxxxxx
GITHUB_DEFAULT_REPO=your-org/your-repo
WORKSPACE_ROOT=/Users/you/Desktop/Projects
CODING_REPO_PATH=/Users/you/Desktop/Projects/your-repo
ANOTHER_CODER_API_KEY=<paste output of the command below>
```

Generate the bridge key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the same value into the AnotherAgent platform → **Settings → Integrations → another_coder → Connect** alongside your ngrok URL + repo path. The bridge then accepts `X-Coder-Key: <that-value>` on every request.

#### Why `WORKSPACE_ROOT` matters

Every Claude Code spawn includes `--dangerously-skip-permissions`. With it, Claude can read, write, and run shell anywhere the spawn's `cwd` points. The bridge enforces that `cwd` lives inside `WORKSPACE_ROOT` (with symlink-escape resolution). Without `WORKSPACE_ROOT` set, the bridge falls back to a bare "is it a directory?" check — Claude can spawn anywhere the uvicorn user can read, including `$HOME`, `~/.ssh`, `/etc`. **Set `WORKSPACE_ROOT` for any deployment exposed via ngrok.**

#### NVM / nodenv / asdf users

Set `CLAUDE_BIN_PATH` in `.env` to the absolute path of the `claude` binary. Daemonized uvicorn processes inherit a stripped PATH that usually doesn't include `~/.nvm/versions/node/<v>/bin`, so leaving this empty triggers `[Errno 2]` after a host reboot.

```bash
which claude        # → /Users/you/.nvm/versions/node/v22.18.0/bin/claude
```

#### Optional tunables

Defaults in [`.env.example`](.env.example) work for a small deployment. The ones worth knowing:

| Var | Default | Purpose |
|---|---|---|
| `BASE_BRANCH` | `dev` | What agent PRs target. Set to `main` if you don't have a `dev` branch. |
| `ANOTHER_CODER_SESSION_DB_PATH` | `~/.another_coder/sessions.db` | SQLite store for conversation_id → Claude Code session_id. Survives restarts. |
| `ANOTHER_CODER_JOB_TTL_SECONDS` | `3600` | TTL reaper drops finished jobs older than this. |
| `ANOTHER_CODER_SESSION_TTL_SECONDS` | `604800` | TTL reaper drops sessions inactive longer than this. |
| `ANOTHER_CODER_REAPER_INTERVAL_SECONDS` | `600` | How often the reaper wakes. |
| `ANOTHER_CODER_RATE_LIMIT_CHAT_STREAM` | `30/minute` | Per-key cap on `/chat/stream`. |
| `ANOTHER_CODER_RATE_LIMIT_INSTRUCT` | `10/minute` | Per-key cap on `/tools/instruct_*`. |
| `ANOTHER_CODER_RATE_LIMIT_JOBS` | `600/minute` | Per-key cap on `/jobs/*`. Stays generous because the platform polls. |
| `ANOTHER_CODER_RATE_LIMIT_AUTH_VERIFY` | `60/minute` | Per-key cap on `/auth/verify`. |

### 3. Run the server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Verify in another terminal:

```bash
curl http://127.0.0.1:8000/health
# {"ok":true,"service":"another_bridge","version":"0.1.0",
#  "claude_probe":{"ok":true,"detail":"claude-code 1.x.y"},
#  "session_store_reachable":true}
```

### 4. Expose via ngrok

```bash
ngrok http 8000
```

Copy the HTTPS URL (e.g. `https://abc-123.ngrok-free.dev`).

### 5. Wire the platform

In the AnotherAgent dashboard:

- **Settings → Integrations → another_coder → Connect** with:
  - `URL` = your ngrok URL
  - `Repo path` = the absolute path you set as `CODING_REPO_PATH`
  - `API key` = the same value you put in `.env`'s `ANOTHER_CODER_API_KEY`
- The platform probes `/auth/verify` against your bridge before saving — a wrong key fails fast with a clear message.

For the planning + implementation flow:

- **Templates → Install built-in → "Engineering Manager Agent"**.
- The webhook tools auto-resolve their URLs from your `another_coder` integration. No per-tool URL pasting needed.

For the **claude_code engine bridge** (chat with Claude Code itself):

- Any agent with `llmConfig.llmEngine = "claude_code"` will route through the bridge automatically. The "Local Claude Code Agent" built-in template is one example; create your own from any other agent's template.

---

## Use it

Open a chat with the Engineering Manager Agent (mobile or web):

```
Plan ticket #<some open issue>
```

Wait ~3 minutes. Then:

```
Go ahead, implement it
```

Wait ~3-10 minutes. A real PR appears on GitHub, branched from `BASE_BRANCH`.

For the bridge: open a chat with any `claude_code`-engine agent and just chat. Conversation continuity is preserved across uvicorn restarts via the SQLite session store, so a mid-day deploy doesn't drop your in-flight conversation.

---

## API surface

| Surface | Route | Auth | Rate limit |
|---|---|---|---|
| Bridge | `POST /chat/stream` (SSE) | X-Coder-Key | 30/min |
| Webhook | `POST /tools/instruct_planning` | X-Coder-Key | 10/min |
| Webhook | `POST /tools/instruct_implementation` | X-Coder-Key | 10/min |
| Webhook | `POST /tools/github_search_issues` | X-Coder-Key | — |
| Webhook | `POST /tools/github_get_issue` | X-Coder-Key | — |
| Webhook | `POST /tools/list_repos` | X-Coder-Key | — |
| Polling | `GET /jobs/{id}/status` | X-Coder-Key | 600/min |
| Polling | `GET /jobs/{id}/chat/status` | X-Coder-Key | 600/min |
| Polling | `POST /jobs/{id}/cancel` | X-Coder-Key | 600/min |
| Probe | `GET /auth/verify` | X-Coder-Key | 60/min |
| Health | `GET /health` | — (open) | — |

Auth is checked before any handler runs (router-level FastAPI `Depends(verify_api_key)`). Rate limiting uses `slowapi`, keyed on the `X-Coder-Key` header (with remote-IP fallback). CORS is intentionally **not** wired — this is a server-to-server bridge, not a browser-facing API.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — sequence diagrams of the bridge SSE flow, the webhook polling flow, the TTL reaper, the persistence layout.
- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, recommended deployment posture, key-rotation procedure. Read this before exposing the bridge over ngrok.
- [`docs/CURL_EXAMPLES.md`](docs/CURL_EXAMPLES.md) — raw cURL probes for every route.

---

## What you get

**Persistence and reliability:**
- **Restart-safe sessions.** `conversation_id → Claude Code session_id` lives in SQLite. A mid-day uvicorn restart resumes every active conversation into the same Claude session via `--resume`.
- **TTL reaper.** Daemon thread prunes done/failed jobs and stale sessions on a slow timer; long-lived processes don't accumulate dead state forever.
- **Structured error kinds.** `spawn_failed`, `timeout`, `cancelled`, `claude_failed`, `git_push_failed`, `pr_create_failed` surface on `/jobs/{id}/status` so the platform can render different UI per failure mode.

**Security:**
- **`WORKSPACE_ROOT` enforcement** on every spawn endpoint — Claude Code (with `--dangerously-skip-permissions`) can't run outside your configured workspace, regardless of what `repo_path` the caller sends. Symlink escape resolved via `os.path.realpath` before the bounds check.
- **Per-key rate limits** on the public ngrok surface so a leaked secret can't drain Claude credits or pin host CPU before rotation.
- **`/auth/verify` probe** so the platform can validate the key before saving the integration — wrong keys fail at Connect time, not at first chat.

**Operational tunables:**
- `pydantic-settings`-typed config — bad env values fail loudly at boot, not at first request.
- Per-request `timeout_seconds` override on planning + implementation webhooks (capped at 1800s).
- Configurable rate-limit specs per surface (empty string disables a given limit).

---

## Troubleshooting

### Bridge / chat issues

**`webhook returned 502 Bad Gateway`** — ngrok tunnel down. Re-run `ngrok http 8000`; the URL changed unless you have a reserved domain. Update the platform's `another_coder` integration with the new URL.

**`bridge unreachable` or 503 from `/auth/verify`** — `ANOTHER_CODER_API_KEY` is unset on the bridge. Check `.env`, restart `uvicorn`.

**`[Errno 2] No such file or directory: 'claude'`** — uvicorn's PATH doesn't have `claude`. Set `CLAUDE_BIN_PATH=/Users/you/.nvm/versions/node/<v>/bin/claude` (or wherever `which claude` says).

**Chat starts a fresh session every turn** — the SQLite session store didn't capture the previous run's `session_id`. Most common cause: the previous run errored or was cancelled (failed runs deliberately do NOT persist `session_id` so a retry can start fresh). Check `/jobs/<id>/status` for the failed run's error.

### Webhook / planning issues

**`GITHUB_PAT not configured`** — your `.env` is missing it, or has trailing whitespace. `cat .env | grep GITHUB_PAT`.

**`Resource not accessible by personal access token` (403) on PR creation** — PAT is missing `Pull requests: Read and write`. GitHub → Developer settings → fine-grained tokens → edit → save.

**`422 head invalid`** — branch was pushed but the PR API can't see it. Usually the local clone's origin URL points at a renamed repo. Run `git remote -v` in the repo path; fix with `git remote set-url origin <correct-url>`.

**Plan looks generic / doesn't reference real files** — Claude Code spawned in the wrong cwd. Confirm `CODING_REPO_PATH` is an absolute path to a real directory containing your code; check `cd $CODING_REPO_PATH && ls`.

**`repo_path is outside the workspace root`** — you set `WORKSPACE_ROOT` but `CODING_REPO_PATH` (or the platform-supplied repo_path) doesn't sit under it. Either move the clone, or update `WORKSPACE_ROOT` to be the parent dir.

### Rate-limit issues

**`429 Too Many Requests` during a heavy demo** — the per-route caps in `.env` are too tight. Loosen via env (e.g. `ANOTHER_CODER_RATE_LIMIT_INSTRUCT=30/minute`). The defaults assume a single operator.

---

## Development

```bash
pip install -e ".[dev]"    # editable install + pytest, pytest-cov, httpx
pytest                     # full suite + coverage gate at 70%
pytest --no-cov            # iterate without the coverage gate
pytest tests/test_chat_security.py -v  # run a focused file
```

The test suite uses an in-memory SQLite session store (set via `tests/conftest.py` before any module import) and patches `subprocess.Popen` at the seam — no real Claude Code spawns, no GitHub round-trips, no on-disk state pollution.

CI runs the same suite + coverage gate on every PR to `dev` (`.github/workflows/test.yml`). A regression below 70% source coverage fails the build.

---

## Running in Docker (optional)

Two cases where Docker is the right path: you don't want Python + Node installed directly on your host, or you're deploying somewhere other than your dev machine. Either way it collapses the install matrix into one `docker compose up`.

```bash
cp .env.example .env
# Fill in ANOTHER_CODER_API_KEY, GITHUB_PAT, WORKSPACE_ROOT, etc.

docker compose up --build
```

Two host directories are bind-mounted into the container:

- **`WORKSPACE_ROOT` (1:1)** — read straight from `.env` and mounted at the same path inside the container, so `WORKSPACE_ROOT` is the source of truth in both venv and Docker deployments. Whatever path the platform sends as `repo_path` resolves identically in both — no translation.
- **`$HOME/.claude` → `/root/.claude`** + **`$HOME/.claude.json` → `/root/.claude.json`** — Claude's auth state lives in TWO host locations: a directory (caches, projects) and a top-level config file (session token). Both are mounted so the containerized binary inherits your host's authenticated session and you don't `claude login` per restart. Override with `HOST_CLAUDE_DIR` / `HOST_CLAUDE_CONFIG` if your CLI auth lives elsewhere. **First-time Docker users:** if you've never run `claude` on this host, both files are missing — Docker auto-creates them as empty dirs (wrong type) and the container errors. Run `claude login` once on host **OR** `touch ~/.claude.json && mkdir -p ~/.claude` before `docker compose up`, then run `docker compose run --rm another_bridge claude login` for the actual auth.

The session DB lives in a named Docker volume (`sessions`) so conversation continuity survives `docker compose down`. See [`docker-compose.yml`](docker-compose.yml) for the full layout + the [`Dockerfile`](Dockerfile) for the build details.

#### First-run: claude authentication

Even with Docker handling Python + Node, the `claude` CLI still needs to log in once — that's an Anthropic-account credential, not something Docker can bake in. **On macOS specifically**, the host's claude stores its OAuth token in the macOS Keychain, which Linux containers can't reach. The bind mount of `~/.claude.json` only carries metadata, not the actual token. So even if you've already run `claude login` on your Mac, the container will still report "Not logged in."

The fix is a one-time login *inside* the container:

```bash
docker compose run --rm another_bridge claude login
```

(The `--rm` flag auto-deletes the one-shot container after the command exits — the auth token persists via the bind mount, so the container itself isn't worth keeping.)

The login flow gives you a URL + a code. Open the URL in any browser, authenticate, paste the code back. Linux claude inside the container falls back to file-based token storage when no system keychain is available, so the token lands in `/root/.claude.json` — which is the bind-mounted host path. Every subsequent `docker compose up` reuses it.

Linux/WSL hosts: if you've already run `claude login` on the host (file-based auth there too), the bind mount may pick it up directly. Try `docker compose up` first; only fall back to the in-container login if you see "Not logged in."

**Laptop-dev recommendation:** if you're testing on a Mac, the venv path above is significantly simpler — no Keychain isolation, no per-container login dance. Docker earns its keep when you're deploying to a VPS or home server, not for local iteration.

#### Windows users

The `$HOME/Projects` and `$HOME/.claude` defaults assume Unix-style paths. On Windows (Docker Desktop with WSL2), set `WORKSPACE_ROOT` in `.env` to your actual workspace path and override `HOST_CLAUDE_DIR` in the shell if needed:

```powershell
# In .env: WORKSPACE_ROOT=C:\Users\you\Projects
$env:HOST_CLAUDE_DIR = "C:\Users\you\.claude"
docker compose up --build
```

The container itself is always Linux — only the host-side bind-mount paths differ.

---

## License

MIT — see [`LICENSE`](LICENSE). Forks are welcome; if you build something interesting on top of the bridge, a PR back is appreciated but not required.
