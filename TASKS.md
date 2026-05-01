# TASKS — another_coder

## Now — pre-publish

- [x] **Add shared-secret header auth on webhook + bridge routes.** Single `X-Coder-Key` header, checked via a FastAPI `Depends` dependency, applied router-level to `/chat/stream`, `/tools/*`, and `/jobs/*`. Constant-time compare (`hmac.compare_digest`) against env var `ANOTHER_CODER_API_KEY`. Reject with 401 on missing/mismatch, 503 when the env var is unset (refuses to silently allow-all). `/health` stays unauthed. Header name is intentionally distinct from the AnotherAgent platform's own `X-API-Key` (which scopes the tenant) so a single hop platform → bridge can carry both without collision.

- [x] **`WORKSPACE_ROOT` enforcement on `/chat/stream`** (commit `1df618f`). The chat endpoint was previously the only spawn surface that did NOT call `validate_repo_path`, so a leaked `X-Coder-Key` could spawn Claude Code in `$HOME` / `~/.ssh` / `/etc`. Now bounded identically to `/tools/instruct_*` — symlink-escape resolved via `os.path.realpath` before the bounds check. Legacy isdir-only fallback preserved for deployments that never set `WORKSPACE_ROOT`.

- [x] **Per-route rate limiting via slowapi** (commit `e1d2d1a`). Bounds the leaked-key blast radius even before the operator rotates. Defaults: 30/min `/chat/stream`, 10/min `/tools/instruct_*`, 600/min `/jobs/*`, 60/min `/auth/verify`. Tunable via `ANOTHER_CODER_RATE_LIMIT_*`. Closes the "rate limit per key" item that was originally in the "Later" backlog — turned out to be small enough to land pre-publish.

- [x] **Wire `CLAUDE_BIN_PATH`** (commit `1df618f`). Previously documented in `.env.example` but ignored — `claude_runner` invoked the bare string `"claude"`. NVM users hit `[Errno 2]` after a uvicorn restart because daemonized processes inherit a stripped PATH.

- [x] **`SECURITY.md`** (commit `d1b8d9f`). Threat model + recommended deployment posture + rotation drill, linked from README + ARCHITECTURE.md.

- [x] **`pyproject.toml` migration.** Replaces `requirements.txt` + `requirements-dev.txt` with PEP 621 metadata. Single source of truth for project name, version (`0.1.0`), runtime deps, dev/test extras (`[dev]`), Python compatibility, classifiers. Dockerfile + CI + README updated to install via `pip install .` / `pip install -e ".[dev]"`. Editable install verified locally (`pip install -e ".[dev]"` succeeds, `importlib.metadata.version('another_coder')` returns `0.1.0`, full suite passes).

## Before Publish

The remaining items I'd want closed before the repo goes public on
YouTube. Ranked by "embarrassment risk if a viewer hits it on day
one" — Tier 1 are the ones I'd refuse to publish without; Tier 3
are quality-of-life polish.

### Tier 1 — would embarrass if a viewer hit it

- [ ] **Smoke-test `docker compose build` end-to-end.** The Dockerfile + docker-compose.yml shipped in commit `1e2a506` were written without a build verification. If the multi-stage `claude_cli` → `bridge` flow has a typo, the first viewer who follows the Docker path hits an immediate failure. ~5 min: clone fresh, `docker compose build`, fix any image-tag drift (`node:20-bookworm-slim` etc. occasionally rename) or apt package-list issues. Worth running once locally before publish.

- [ ] **Smoke-test the GitHub Actions workflow.** `.github/workflows/test.yml` was authored on faith. YAML syntax errors, action-version drift, missing setup steps don't surface until a real PR fires the workflow. Either run `act` locally OR open a no-op PR after publish and verify the green checkmark before announcing. Failing first PR = bad first impression for would-be contributors.

- [ ] **Add `__version__` lookup + expose in `/health`.** `pyproject.toml` now has `version = "0.1.0"` but the runtime never reads it. `routers/health.py` returns `{ok: true, service: "another_coder"}` — useless for bug triage. Should additionally expose `version` (via `importlib.metadata.version("another_coder")`), the result of the boot-time claude probe (was the binary invocable? what version?), and a session-store reachable boolean. ~30 lines including a test. Without this, every bug report gets "what version are you on?" with no answer.

### Tier 2 — strongly recommended before publish

- [ ] **`CHANGELOG.md`.** Public projects need this. Even a single `## [0.1.0] - <date>` entry summarizing the current state (Phase 2 + 3 (1) + 4 + Sprint 2 + the security hardening) gives future updates a baseline. Future bumps reference back to this.

- [ ] **`CONTRIBUTING.md` stub.** ~60 lines covering: how to run tests (`pip install -e ".[dev]" && pytest`), the dev branch + PR-to-`dev` flow, the coverage gate, where to file security issues (pointer to SECURITY.md), expected commit-message conventions. Sets the bar so the first external PRs aren't messy.

- [ ] **`docs/CURL_EXAMPLES.md` refresh.** The file is 15 lines and predates everything we shipped this session. Should cover the new public surfaces: `/auth/verify` probe, `/jobs/<id>/chat/status` (the polling-reconnect endpoint), the rate-limit 429 response shape, and the structured `error_kind` field on `/jobs/<id>/status`.

### Tier 3 — polish, post-publish if time runs short

- [ ] **README hero image / GIF.** A 5-second loop of "phone unlocks → chat with Claude Code → file edited on laptop" would be the GitHub thumbnail. Massively shifts whether someone scrolls or bounces. Only useful AFTER Video 1 is recorded — until then there's no source footage.

- [ ] **Pre-commit hooks** (ruff + black + mypy stub). Style + import-order consistency for contributors. Optional; pytest is the load-bearing gate.

- [ ] **OpenAPI spec generator for the bridge.** The platform (`another_agent_backend`) already has one (we synced it earlier). The bridge has no equivalent — external consumers have to read source. FastAPI auto-generates one at `/docs` when running, but a published JSON/YAML spec for offline reference would be useful for SDK generators.

- [ ] **`pyproject.toml` URL placeholders are still `another_coder/another_coder`.** Replace with the real GitHub org/repo once the public repo URL is known. Grep target so this doesn't get missed: `grep -n "another_coder/another_coder" pyproject.toml`.

- [ ] **Enriched `/ready` endpoint** (separate from `/health`). `/health` is liveness ("the process is up"); `/ready` would be the meaningful "ready to serve real traffic" check (claude probe ok + session store responsive + reaper alive). Useful for k8s-style deployments — overkill for a laptop or single-VPS bridge. Defer until a real ops user asks.

## Later (only if a real user asks)

- [ ] Per-tool keys (rotate independently).
- [ ] Audit log of authed calls.

These two stay deferred — they buy little for a single-operator deployment and would meaningfully complicate the auth surface. Reopen if a multi-user / shared-bridge use case appears.
