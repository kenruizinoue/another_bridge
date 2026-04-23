import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_DEFAULT_REPO = os.environ.get("GITHUB_DEFAULT_REPO", "")
GITHUB_API_BASE = "https://api.github.com"

CODING_REPO_PATH = os.environ.get("CODING_REPO_PATH", "")

TARGET_DIRECTORY = os.path.expanduser("~/Desktop/another_logic_backend")
PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profiles", "npm_install.txt")
CLAUDE_MODEL = "claude-sonnet-4-6"
ANOTHER_LOGIC_API_KEY = os.environ.get("ANOTHER_LOGIC_API_KEY", "")
ANOTHER_LOGIC_BASE_URL = os.environ.get("ANOTHER_LOGIC_BASE_URL", "http://localhost:3000")
ASK_PROFILE_ID = "69bee9d1f38c71f60bd3ce10"
