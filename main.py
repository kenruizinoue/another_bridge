import os
import subprocess
from fastapi import FastAPI

app = FastAPI()

TARGET_DIRECTORY = os.path.expanduser("~/Desktop/another_logic_backend")


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/implementNextCommit")
def implement_next_commit():
    ls_result = subprocess.run(["ls", TARGET_DIRECTORY], capture_output=True, text=True)
    if ls_result.returncode != 0:
        return {"error": f"Cannot open directory: {ls_result.stderr.strip()}"}

    files = ls_result.stdout.splitlines()
    if "CLAUDE.md" not in files:
        return {"error": "CLAUDE.md not found in target directory"}

    claude_result = subprocess.run(
        ["claude", "-p", "explain about this project", "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        cwd=TARGET_DIRECTORY,
    )

    if claude_result.returncode != 0:
        return {"error": claude_result.stderr.strip()}

    return {"claude_response": claude_result.stdout.strip()}
