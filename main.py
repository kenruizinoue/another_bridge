import logging

import structlog
from fastapi import FastAPI

from config import (
    ANOTHER_LOGIC_API_KEY,
    ANOTHER_LOGIC_BASE_URL,
    ASK_PROFILE_ID,
    CLAUDE_MODEL,
    TARGET_DIRECTORY,
)
from routers import chat, github, health, implementation, jobs, legacy, planning, repos

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = structlog.get_logger()

app = FastAPI()


@app.on_event("startup")
def on_startup():
    log.info(
        "server.started",
        target_directory=TARGET_DIRECTORY,
        model=CLAUDE_MODEL,
        ask_profile_id=ASK_PROFILE_ID,
        base_url=ANOTHER_LOGIC_BASE_URL,
        another_logic_api_key=ANOTHER_LOGIC_API_KEY,
    )


app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(github.router)
app.include_router(repos.router)
app.include_router(planning.router)
app.include_router(implementation.router)
app.include_router(legacy.router)
# Chat streaming bridge — POST /chat/stream forwards a turn to a local
# Claude Code subprocess and streams text back as SSE. Used by the
# platform's claude_code engine bridge (see another_agent_backend's
# src/graphs/nodes/generateResponse/bridge.ts).
app.include_router(chat.router)
