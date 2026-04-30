# another_coder

FastAPI webhook host for the AnotherAgent **Coder Team** template. Exposes the four webhook tools the platform calls to read GitHub, plan tickets via Claude Code, and implement plans into real PRs.

When wired up, you can chat the Engineering Manager Agent from your phone:

> *"Plan ticket #41"* → plan returned
> *"Go ahead, implement it"* → real PR opened on GitHub

---

## Prerequisites

Install these once:

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/quickstart)** — `npm install -g @anthropic-ai/claude-code` then `claude` should be on your PATH
- **[ngrok](https://ngrok.com/download)** — free account is enough
- **A GitHub fine-grained PAT** with these permissions on the target repo:
  - `Issues: Read`
  - `Pull requests: Read and write`
  - `Contents: Read and write`
- **A local clone of the target repo** (the one whose tickets you want to implement)
- **An AnotherAgent account** with the platform running (local or hosted)

---

## Setup (5 steps)

### 1. Clone + install

```bash
git clone <this-repo-url> another_coder
cd another_coder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```
GITHUB_PAT=github_pat_xxxxxxxxxxxx
GITHUB_DEFAULT_REPO=your-org/your-repo
CODING_REPO_PATH=/absolute/path/to/your/local/clone
BASE_BRANCH=dev
ANOTHER_CODER_API_KEY=<paste output of the command below>
```

`BASE_BRANCH` is what agent PRs target. Defaults to `dev`. Change to `main` if your repo doesn't use a dev branch.

`ANOTHER_CODER_API_KEY` is a shared secret callers must send as `X-Coder-Key`. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The same value goes into your AnotherAgent agent's **API secret** field (Agent detail → Claude Code → API secret). `/health` stays unauthed for uptime checks; everything else (`/chat/stream`, `/tools/*`, `/jobs/*`) requires the header.

### 3. Run the server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Verify in another terminal:

```bash
curl http://127.0.0.1:8000/health
# {"ok": true, "service": "another_coder"}
```

### 4. Expose via ngrok

In another terminal:

```bash
ngrok http 8000
```

Copy the HTTPS URL ngrok prints (e.g. `https://abc-123.ngrok-free.dev`). You'll paste it in step 5.

### 5. Install + configure the platform template

In your AnotherAgent dashboard:

1. **Templates → Install built-in → "Engineering Manager Agent"** (the Coder Team bundle).
2. After install, open each of the 4 webhook tools:
   - `github_search_issues`
   - `github_get_issue`
   - `instruct_planning`
   - `instruct_implementation`
3. Replace `https://YOUR-NGROK-SUBDOMAIN.ngrok-free.dev` with your real ngrok URL from step 4. Keep the `/tools/...` path.
4. Save each tool.

Done. The Engineering Manager Agent is now ready.

---

## Use it

Open a chat with the Engineering Manager Agent (mobile or web). Try:

```
Plan ticket #<some open issue number>
```

Wait ~3 minutes for the plan to come back. Then:

```
Go ahead, implement it
```

Wait ~3-10 minutes. A real PR appears on GitHub, branched from `dev`, ready for your review.

---

## Webhook tools

| Endpoint | What it does |
|---|---|
| `POST /tools/github_search_issues` | Lists candidate tickets via GitHub REST |
| `POST /tools/github_get_issue` | Fetches one ticket's body + comments |
| `POST /tools/instruct_planning` | Spawns Claude Code in the repo, returns a plan grounded in real files |
| `POST /tools/instruct_implementation` | Branches from `BASE_BRANCH`, implements the plan, commits, pushes, opens a PR |

All accept `{ "arguments": { ... } }` payloads matching the Engineering Manager Agent template's tool schemas.

---

## Troubleshooting

### `webhook returned 502 Bad Gateway` from the platform
ngrok tunnel is down. Re-run `ngrok http 8000` and verify the URL is the same as in the platform tool configs.

### `GITHUB_PAT not configured` error from the webhook
Your `.env` is missing `GITHUB_PAT`, or the value has whitespace / quotes. Check `cat .env` — no trailing spaces.

### `Resource not accessible by personal access token` (403) on PR creation
PAT is missing `Pull requests: Read and write`. Go to GitHub → Developer settings → Fine-grained tokens → edit token → save permissions.

### `not all refs are readable` (422) on PR creation
PAT is missing `Contents: Read and write` (despite the misleading error). Same fix as above — also confirm the token covers the target repo under "Selected repositories."

### Plan looks generic / doesn't reference real files
Claude Code doesn't have access to the repo. Check `CODING_REPO_PATH` is an absolute path to a real directory and that running `cd $CODING_REPO_PATH && ls` shows your repo files.

### Engineering Manager doesn't delegate "implement it" to the Developer
Check the platform backend is on a recent build that includes the `resolveConversation.ts` history-trim fix. Old builds keep the OLDEST 6 messages instead of the newest, so the plan turn falls out of context.

---

## Endpoints (also legacy)

- `GET /health` — v1 healthcheck
- `POST /tools/*` — v1 webhook tools (above)
- `GET /verifyApiKey` — legacy, will be removed
- `POST /implementTicket` — legacy, will be removed

See [docs/CURL_EXAMPLES.md](docs/CURL_EXAMPLES.md) for raw cURL examples.

---

## Architecture (brief)

```
You (phone)
  ↓ chat
AnotherAgent platform — Engineering Manager Agent
  ↓ delegates
Planner / Developer Agents
  ↓ webhook tool calls
ngrok tunnel
  ↓
another_coder (this FastAPI app)
  ↓ subprocess
claude -p (Claude Code CLI in your local repo)
  ↓
git push + GitHub PR API
  ↓
PR opens on GitHub
```

Three Claude/LLM layers cooperate: gpt-4o-mini for the platform agents (routing + tool selection), and Claude Code (claude-sonnet-4-6) for actual repo work.
