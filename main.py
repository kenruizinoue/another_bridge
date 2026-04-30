import logging

import structlog
from fastapi import Depends, FastAPI

from auth import verify_api_key
from config import CLAUDE_MODEL
from routers import auth as auth_router, chat, github, health, implementation, jobs, planning, repos

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = structlog.get_logger()

app = FastAPI()


@app.on_event("startup")
def on_startup():
    log.info("server.started", model=CLAUDE_MODEL)


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
