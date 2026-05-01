# TASKS — another_coder

## Now — pre-publish

- [x] **Add shared-secret header auth on webhook + bridge routes.** Single `X-Coder-Key` header, checked via a FastAPI `Depends` dependency, applied router-level to `/chat/stream`, `/tools/*`, and `/jobs/*`. Constant-time compare (`hmac.compare_digest`) against env var `ANOTHER_CODER_API_KEY`. Reject with 401 on missing/mismatch, 503 when the env var is unset (refuses to silently allow-all). `/health` stays unauthed. Header name is intentionally distinct from the AnotherAgent platform's own `X-API-Key` (which scopes the tenant) so a single hop platform → bridge can carry both without collision.

- [x] **`WORKSPACE_ROOT` enforcement on `/chat/stream`** (commit `1df618f`). The chat endpoint was previously the only spawn surface that did NOT call `validate_repo_path`, so a leaked `X-Coder-Key` could spawn Claude Code in `$HOME` / `~/.ssh` / `/etc`. Now bounded identically to `/tools/instruct_*` — symlink-escape resolved via `os.path.realpath` before the bounds check. Legacy isdir-only fallback preserved for deployments that never set `WORKSPACE_ROOT`.

- [x] **Per-route rate limiting via slowapi** (commit `e1d2d1a`). Bounds the leaked-key blast radius even before the operator rotates. Defaults: 30/min `/chat/stream`, 10/min `/tools/instruct_*`, 600/min `/jobs/*`, 60/min `/auth/verify`. Tunable via `ANOTHER_CODER_RATE_LIMIT_*`. Closes the "rate limit per key" item that was originally in the "Later" backlog — turned out to be small enough to land pre-publish.

- [x] **Wire `CLAUDE_BIN_PATH`** (commit `1df618f`). Previously documented in `.env.example` but ignored — `claude_runner` invoked the bare string `"claude"`. NVM users hit `[Errno 2]` after a uvicorn restart because daemonized processes inherit a stripped PATH.

- [x] **`SECURITY.md`** (commit `d1b8d9f`). Threat model + recommended deployment posture + rotation drill, linked from README + ARCHITECTURE.md.

## Later (only if a real user asks)

- [ ] Per-tool keys (rotate independently).
- [ ] Audit log of authed calls.

These two stay deferred — they buy little for a single-operator deployment and would meaningfully complicate the auth surface. Reopen if a multi-user / shared-bridge use case appears.
