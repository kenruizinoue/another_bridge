import os
import subprocess
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

TARGET_DIRECTORY = os.path.expanduser("~/Desktop/another_logic_backend")
PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profiles", "npm_install.txt")
CLAUDE_MODEL = "claude-sonnet-4-6"


class TicketRequest(BaseModel):
    ticket: str


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/implementTicket")
def implement_ticket(body: TicketRequest):
    ticket = body.ticket

    ls_result = subprocess.run(["ls", TARGET_DIRECTORY], capture_output=True, text=True)
    if ls_result.returncode != 0:
        return {"error": f"Cannot open directory: {ls_result.stderr.strip()}"}

    files = ls_result.stdout.splitlines()
    if "CLAUDE.md" not in files:
        return {"error": "CLAUDE.md not found in target directory"}

    with open(PROFILE_FILE) as f:
        profile = f.read().strip()

    claude_result = subprocess.run(
        ["claude", "-p", f"implement this ticket: {ticket}\n\nIMPORTANT: {profile}", "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        cwd=TARGET_DIRECTORY,
    )

    if claude_result.returncode != 0:
        return {"error": claude_result.stderr.strip()}

    return {"claude_response": claude_result.stdout.strip()}
