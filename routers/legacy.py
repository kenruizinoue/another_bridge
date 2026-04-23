import subprocess

import requests
import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import (
    ANOTHER_LOGIC_API_KEY,
    ANOTHER_LOGIC_BASE_URL,
    ASK_PROFILE_ID,
    CLAUDE_MODEL,
    PROFILE_FILE,
    TARGET_DIRECTORY,
)

log = structlog.get_logger()

router = APIRouter()

PREVIOUS_RESULT = "Nothing"


@router.get("/verifyApiKey")
def verify_api_key():
    log.info("verify_api_key", another_logic_api_key=ANOTHER_LOGIC_API_KEY)
    return {"another_logic_api_key": ANOTHER_LOGIC_API_KEY}


@router.post("/implementTicket")
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

    def stream_claude():
        global PREVIOUS_RESULT
        proc = subprocess.Popen(
            ["claude", "-p", f"implement this ticket: {ticket}\n\nIMPORTANT: {profile}", "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=TARGET_DIRECTORY,
        )

        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log.info("claude.output", line=line)
                yield f"data: {line}\n\n"

        proc.wait()

        if proc.returncode != 0:
            stderr = proc.stderr.read().strip()
            log.error("implement_ticket.claude_failed", returncode=proc.returncode, stderr=stderr)
            yield f"data: ERROR: {stderr}\n\n"
        else:
            PREVIOUS_RESULT = ticket
            log.info("implement_ticket.done", new_previous_result=PREVIOUS_RESULT)

        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_claude(), media_type="text/event-stream")
