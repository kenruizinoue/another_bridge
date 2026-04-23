import os
import subprocess
from typing import Any
import requests
import structlog
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

load_dotenv()

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = structlog.get_logger()

app = FastAPI()

TARGET_DIRECTORY = os.path.expanduser("~/Desktop/another_logic_backend")
PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profiles", "npm_install.txt")
CLAUDE_MODEL = "claude-sonnet-4-6"
ANOTHER_LOGIC_API_KEY = os.environ.get("ANOTHER_LOGIC_API_KEY", "")
ANOTHER_LOGIC_BASE_URL = os.environ.get("ANOTHER_LOGIC_BASE_URL", "http://localhost:3000")
ASK_PROFILE_ID = "69bee9d1f38c71f60bd3ce10"

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_DEFAULT_REPO = os.environ.get("GITHUB_DEFAULT_REPO", "")
GITHUB_API_BASE = "https://api.github.com"

PREVIOUS_RESULT = "Nothing"


@app.on_event("startup")
def on_startup():
    log.info("server.started", target_directory=TARGET_DIRECTORY, model=CLAUDE_MODEL, ask_profile_id=ASK_PROFILE_ID, base_url=ANOTHER_LOGIC_BASE_URL, another_logic_api_key=ANOTHER_LOGIC_API_KEY)


@app.get("/verifyApiKey")
def verify_api_key():
    log.info("verify_api_key", another_logic_api_key=ANOTHER_LOGIC_API_KEY)
    return {"another_logic_api_key": ANOTHER_LOGIC_API_KEY}


@app.get("/health")
def health():
    return {"ok": True, "service": "another_coder"}


@app.post("/tools/github_search_issues")
async def github_search_issues(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}

    repo = args.get("repo") or GITHUB_DEFAULT_REPO
    label = args.get("label")
    state = args.get("state") or "open"

    log.info("github_search_issues.request", repo=repo, label=label, state=state)

    if not GITHUB_PAT:
        log.error("github_search_issues.missing_pat")
        return {"error": "GITHUB_PAT not configured in another_coder/.env"}
    if not repo:
        log.error("github_search_issues.missing_repo")
        return {"error": "repo not provided and GITHUB_DEFAULT_REPO not configured"}

    params: dict[str, Any] = {"state": state, "per_page": 30}
    if isinstance(label, str) and label.strip():
        params["labels"] = label.strip()

    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    resp = requests.get(f"{GITHUB_API_BASE}/repos/{repo}/issues", headers=headers, params=params, timeout=15)
    if not resp.ok:
        log.error("github_search_issues.api_failed", status=resp.status_code, body=resp.text)
        return {"error": f"GitHub API {resp.status_code}: {resp.text}"}

    raw = resp.json()
    issues = [
        {
            "number": item["number"],
            "title": item["title"],
            "labels": [lbl["name"] for lbl in item.get("labels", [])],
            "url": item["html_url"],
            "state": item["state"],
        }
        for item in raw
        if "pull_request" not in item
    ]

    log.info("github_search_issues.ok", count=len(issues), repo=repo)
    return {"issues": issues, "count": len(issues), "repo": repo}


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
