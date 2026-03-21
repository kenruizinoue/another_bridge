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
    result = subprocess.run(["ls", TARGET_DIRECTORY], capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": f"Cannot open directory: {result.stderr.strip()}"}

    files = result.stdout.splitlines()
    has_claude_md = "CLAUDE.md" in files

    return {
        "directory": TARGET_DIRECTORY,
        "claude_md_found": has_claude_md,
    }
