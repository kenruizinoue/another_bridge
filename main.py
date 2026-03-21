import os
import subprocess
import requests
import structlog
import logging
from fastapi import FastAPI

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = structlog.get_logger()

app = FastAPI()

TARGET_DIRECTORY = os.path.expanduser("~/Desktop/another_logic_backend")
PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profiles", "npm_install.txt")
CLAUDE_MODEL = "claude-sonnet-4-6"
ANOTHER_LOGIC_API_KEY = os.environ.get("ANOTHER_LOGIC_API_KEY", "")
ANOTHER_LOGIC_BASE_URL = os.environ.get("ANOTHER_LOGIC_BASE_URL", "http://localhost:3000")
ASK_PROFILE_ID = "69bee9d1f38c71f60bd3ce10"

PREVIOUS_RESULT = "Nothing"


@app.on_event("startup")
def on_startup():
    log.info("server.started", target_directory=TARGET_DIRECTORY, model=CLAUDE_MODEL, ask_profile_id=ASK_PROFILE_ID, base_url=ANOTHER_LOGIC_BASE_URL, another_logic_api_key=ANOTHER_LOGIC_API_KEY)


@app.get("/verifyApiKey")
def verify_api_key():
    log.info("verify_api_key", another_logic_api_key=ANOTHER_LOGIC_API_KEY)
    return {"another_logic_api_key": ANOTHER_LOGIC_API_KEY}


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/implementTicket")
def implement_ticket():
    global PREVIOUS_RESULT

    log.info("implement_ticket.started", previous_result=PREVIOUS_RESULT)

    ls_result = subprocess.run(["ls", TARGET_DIRECTORY], capture_output=True, text=True)
    if ls_result.returncode != 0:
        log.error("implement_ticket.directory_check_failed", error=ls_result.stderr.strip())
        return {"error": f"Cannot open directory: {ls_result.stderr.strip()}"}

    files = ls_result.stdout.splitlines()
    if "CLAUDE.md" not in files:
        log.error("implement_ticket.claude_md_missing", directory=TARGET_DIRECTORY)
        return {"error": "CLAUDE.md not found in target directory"}

    log.info("implement_ticket.calling_ask", profile_id=ASK_PROFILE_ID, message=f"I have implemented: {PREVIOUS_RESULT}")
    ask_response = requests.post(
        f"{ANOTHER_LOGIC_BASE_URL}/ask",
        headers={"Content-Type": "application/json", "X-API-Key": ANOTHER_LOGIC_API_KEY},
        json={"profileId": ASK_PROFILE_ID, "message": f"I have implemented: {PREVIOUS_RESULT}"},
    )
    if not ask_response.ok:
        log.error("implement_ticket.ask_failed", status_code=ask_response.status_code, response=ask_response.text)
        return {"error": f"/ask call failed: {ask_response.text}"}

    ticket = ask_response.json()["message"]
    log.info("implement_ticket.ticket_received", ticket=ticket)

    with open(PROFILE_FILE) as f:
        profile = f.read().strip()

    log.info("implement_ticket.running_claude", model=CLAUDE_MODEL, cwd=TARGET_DIRECTORY)
    claude_result = subprocess.run(
        ["claude", "-p", f"implement this ticket: {ticket}\n\nIMPORTANT: {profile}", "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        cwd=TARGET_DIRECTORY,
    )

    if claude_result.returncode != 0:
        log.error("implement_ticket.claude_failed", returncode=claude_result.returncode, stderr=claude_result.stderr.strip())
        return {"error": claude_result.stderr.strip()}

    PREVIOUS_RESULT = ticket
    log.info("implement_ticket.done", new_previous_result=PREVIOUS_RESULT)
    return {"claude_response": claude_result.stdout.strip()}
