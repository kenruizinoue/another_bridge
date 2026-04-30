import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_DEFAULT_REPO = os.environ.get("GITHUB_DEFAULT_REPO", "")
GITHUB_API_BASE = "https://api.github.com"

CODING_REPO_PATH = os.environ.get("CODING_REPO_PATH", "")
BASE_BRANCH = os.environ.get("BASE_BRANCH", "dev")

# Optional. When set, planning + implementation only accept repo_path values
# that resolve to a child of WORKSPACE_ROOT, and list_repos enumerates child
# dirs containing both .git/ and an origin remote. Leave empty to preserve
# the original CODING_REPO_PATH-only flow (no allow-list, no enumeration).
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "")

CLAUDE_MODEL = "claude-opus-4-7"

# Shared secret callers must send as the `X-Coder-Key` header on /chat/stream,
# /tools/*, and /jobs/*. The same value is configured on the AnotherAgent
# side as `agent.llmConfig.coderApiKey`. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# Empty value is treated as a misconfiguration and returns 503 — see
# auth.verify_api_key for the rationale.
ANOTHER_CODER_API_KEY = os.environ.get("ANOTHER_CODER_API_KEY", "")
