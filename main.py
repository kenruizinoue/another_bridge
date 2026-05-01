import logging
from contextlib import asynccontextmanager

# Load .env into os.environ at import time. pydantic-settings already
# reads .env into the Settings object, but it does NOT push values
# back into os.environ — so consumers like auth.py that read
# os.environ.get(...) directly would otherwise see empty values when
# uvicorn is run standalone. Docker's env_file: .env handles this in
# the container path; load_dotenv() handles the venv path. Tests
# don't import main, so this is a no-op there.
from dotenv import load_dotenv

load_dotenv()

import structlog
from fastapi import Depends, FastAPI
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from auth import verify_api_key
from config import CLAUDE_MODEL
from routers import auth as auth_router, chat, github, health, implementation, jobs, planning, repos
from services.claude_runner import probe_claude_binary
from services.rate_limiter import limiter
from services.reaper import build_default_reaper

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = structlog.get_logger()


# `@app.on_event("startup")` is deprecated in FastAPI ≥ 0.93; the
# replacement is a lifespan async context manager. Statements before
# ``yield`` run on startup; statements after run on shutdown.
#
# The reaper is constructed lazily inside the lifespan function (not at
# module import) so unit tests that only ``import main`` don't spawn a
# background thread on every test collection. The TestClient(app)
# fixture used in production-style tests would still trigger startup;
# none of our tests bind to ``main.app`` for that reason.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("server.started", model=CLAUDE_MODEL)
    # Operator UX: probe `claude --version` at boot so a missing /
    # broken CLI surfaces in the startup log rather than at first
    # chat (where it would surface as a generic ``spawn_failed``
    # error_kind). Never crashes the boot — /health and /auth/verify
    # stay useful regardless, and the operator might fix the CLI
    # without a restart (e.g. by re-running `claude login`).
    ok, detail = probe_claude_binary()
    if ok:
        log.info("claude.ready", version=detail)
    else:
        log.warning("claude.not_invocable", reason=detail)
    # Stash on app.state so /health can surface it without re-running
    # the probe per request. Operators on remote deploys (no easy log
    # access) can curl /health to triage a stuck CLI without ssh-ing
    # into the box.
    _app.state.claude_probe = {"ok": ok, "detail": detail}
    reaper = build_default_reaper()
    reaper.start()
    try:
        yield
    finally:
        reaper.stop()


app = FastAPI(lifespan=lifespan)

# Rate limiter wired into FastAPI's state + middleware stack. The
# state binding is what slowapi's @limiter.limit decorators look for
# at request time; SlowAPIMiddleware enforces the buckets and emits
# 429 responses. RateLimitExceeded is also registered as an exception
# handler so the 429 surfaces with slowapi's structured body
# ({error: "rate limited", detail: "30 per 1 minute"}). See
# services/rate_limiter.py for the key strategy + per-route limits
# (those are applied in each router via @limiter.limit("...") on the
# handler functions). CORS is intentionally NOT wired — this is a
# webhook bridge consumed by the platform's server, not a browser,
# so a CORS policy would be misleading.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_exceeded_handler(_request, exc: RateLimitExceeded):
    # Re-export slowapi's default 429 handler in our exception
    # handler registry so a custom error shape can be added later
    # without changing the import order in routers. For now,
    # delegate to slowapi's default which produces:
    #   {"error":"rate limited","detail":"30 per 1 minute"}
    from slowapi import _rate_limit_exceeded_handler as _default

    return _default(_request, exc)


# /health stays unauthed — ngrok / uptime checks consume it without
# needing the bridge secret.
app.include_router(health.router)

# Auth-gated routers. `dependencies=[Depends(verify_api_key)]` runs the
# header check before any handler in the router fires; missing /
# mismatched key returns 401 without ever entering the handler. Applied
# at the include level so router files stay free of auth wiring and
# new routes inherit the gate by default.
_authed = [Depends(verify_api_key)]
# Auth probe — Settings → Integrations → another_coder Connect calls
# /auth/verify before saving the key, so a wrong key fails fast at
# Connect time instead of silently surfacing 401s on the next chat.
app.include_router(auth_router.router, dependencies=_authed)
app.include_router(jobs.router, dependencies=_authed)
app.include_router(github.router, dependencies=_authed)
app.include_router(repos.router, dependencies=_authed)
app.include_router(planning.router, dependencies=_authed)
app.include_router(implementation.router, dependencies=_authed)
# Chat streaming bridge — POST /chat/stream forwards a turn to a local
# Claude Code subprocess and streams text back as SSE. Used by the
# platform's claude_code engine bridge (see another_agent_backend's
# src/graphs/nodes/generateResponse/bridge.ts).
app.include_router(chat.router, dependencies=_authed)
