# another_coder

FastAPI webhook host for AnotherAgent. Exposes tools the platform calls to read GitHub, plan, evaluate, and implement tickets via Claude Code.

## Quickstart

```bash
git clone <repo>
cd another_coder
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in GITHUB_PAT, GITHUB_DEFAULT_REPO, CLAUDE_BIN_PATH, WORKSPACE_DIR
python main.py        # or: uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Expose to the platform:

```bash
ngrok http 8000
```

Use the ngrok HTTPS URL as the `webhookUrl` in your AnotherAgent tool configs.

## Verify

```bash
curl http://127.0.0.1:8000/health
# {"ok": true, "service": "another_coder"}
```

From phone (after ngrok):

```
https://<ngrok-id>.ngrok-free.app/health
```

## Endpoints

- `GET /health` — v1 healthcheck
- `GET /verifyApiKey` — legacy
- `POST /implementTicket` — legacy (will be removed once Milestone 3 lands)

V1 webhook tools (`/tools/*`) land in TICKET-002+.

## Docs

See [docs/CURL_EXAMPLES.md](docs/CURL_EXAMPLES.md).
