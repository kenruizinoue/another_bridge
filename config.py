"""Typed configuration loaded from .env via pydantic-settings.

Replaces the ad-hoc ``os.environ.get`` calls that used to live here.
Two benefits:

  1. Types are enforced at startup. ``ANOTHER_CODER_JOB_TTL_SECONDS=
     "not-a-number"`` now fails the boot loudly instead of silently
     falling back to a string and crashing the first time the reaper
     tries to subtract it. (Plus a guarded fallback in
     ``services/reaper.build_default_reaper`` keeps malformed env
     values from taking down the runtime — defense in depth.)

  2. Tests can construct ``Settings()`` mid-run after a monkeypatch
     to read fresh values without re-importing the module. This
     unblocks the reaper / session_store env-override tests that
     currently rely on ``os.environ.get`` at function-call time.

Backward-compat: every constant the rest of the codebase imported
from this module (``GITHUB_PAT``, ``CLAUDE_MODEL``, ``WORKSPACE_ROOT``,
etc.) is still exported as a module-level name pointing at
``settings.<field>`` — no router or service needs to change its
imports.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level configuration. Reads from environment variables
    and (when present) the ``.env`` file in the project root.

    ``extra="ignore"`` so unrelated entries in .env (or experimental
    vars in dev) don't error the boot. Field names are lowercased
    by convention; pydantic-settings matches them against env vars
    case-insensitively so ``GITHUB_PAT`` populates ``github_pat``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── GitHub ─────────────────────────────────────────────────────
    github_pat: str = ""
    github_default_repo: str = ""
    # Constant — not env-configurable. Lives here so callers can
    # ``from config import GITHUB_API_BASE`` next to the related
    # GitHub fields rather than in some random services/ module.
    github_api_base: str = "https://api.github.com"

    # ── Claude Code ────────────────────────────────────────────────
    claude_model: str = "claude-opus-4-7"
    # Empty / unset → falls back to bare "claude" at the consumer
    # (services/claude_runner.py). Stored verbatim here so a misset
    # value (e.g. trailing whitespace) is visible in logs.
    claude_bin_path: str = ""

    # ── Repo locations ─────────────────────────────────────────────
    workspace_root: str = ""
    coding_repo_path: str = ""
    base_branch: str = "dev"

    # ── Bridge auth ────────────────────────────────────────────────
    # Empty value is treated as a misconfiguration by
    # ``auth.verify_api_key`` (returns 503) — refusing to silently
    # allow-all when the operator forgot to set the secret.
    another_coder_api_key: str = ""

    # ── Persistence ────────────────────────────────────────────────
    # Empty → resolved to ~/.another_coder/sessions.db at consumer
    # time. Tests override to ``:memory:`` via tests/conftest.py
    # before any import triggers the singleton.
    another_coder_session_db_path: str = ""
    another_coder_job_ttl_seconds: int = Field(default=3600, ge=1)
    another_coder_session_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1)
    another_coder_reaper_interval_seconds: int = Field(default=600, ge=1)

    # ── Rate limiting ──────────────────────────────────────────────
    # Public ngrok deployments are gated by a single shared
    # X-Coder-Key, so a leaked key would be a full-shell foothold
    # without these caps. Conservative defaults keyed on the
    # X-Coder-Key (or remote IP fallback). Each limit is a slowapi
    # spec like "60/minute" — empty string disables the limit on
    # that surface. Tunable via env so a heavy demo can loosen
    # without code changes.
    another_coder_rate_limit_chat_stream: str = "30/minute"
    another_coder_rate_limit_instruct: str = "10/minute"
    # /jobs/* MUST stay generous — the platform's status-poll
    # cadence is ~5s per active job + the bridge-status proxy
    # mirrors that, so a single mid-flight Claude run can easily
    # generate 12 GETs per minute.
    another_coder_rate_limit_jobs: str = "600/minute"
    another_coder_rate_limit_auth_verify: str = "60/minute"
    # /sessions is a cheap on-disk index read (cached by mtime), so a
    # mobile card list refreshing on pull-to-refresh won't hammer it.
    another_coder_rate_limit_sessions: str = "120/minute"

    # ── Session browsing ───────────────────────────────────────────
    # Root that Claude Code writes per-conversation JSONL transcripts
    # under (one <encoded-cwd>/ dir per working directory, one
    # <sessionId>.jsonl per conversation). Empty → resolve at import
    # from CLAUDE_CONFIG_DIR (Claude Code's own override) else the
    # ~/.claude default. Exposed so a future test can point it at a
    # fixture tree instead of the real home dir.
    another_coder_claude_projects_dir: str = ""


# Module-level singleton. Constructed at import; the rest of the
# codebase reads frozen values via the back-compat constants below.
# Tests that need fresh values after a monkeypatch instantiate
# ``Settings()`` directly inside the test body.
settings = Settings()


# ── Backward-compat constants ────────────────────────────────────────
# Existing callers do `from config import CLAUDE_MODEL` etc. Keep
# these working without forcing every router/service to switch to
# `settings.foo`. New code should prefer importing `settings`
# directly so the type annotations follow.
GITHUB_PAT: str = settings.github_pat
GITHUB_DEFAULT_REPO: str = settings.github_default_repo
GITHUB_API_BASE: str = settings.github_api_base

CLAUDE_MODEL: str = settings.claude_model
# Empty / whitespace-only → bare "claude" so PATH resolves the binary.
CLAUDE_BIN_PATH: str = settings.claude_bin_path.strip() or "claude"

WORKSPACE_ROOT: str = settings.workspace_root
CODING_REPO_PATH: str = settings.coding_repo_path
BASE_BRANCH: str = settings.base_branch

ANOTHER_CODER_API_KEY: str = settings.another_coder_api_key


# Session store: when the env var is empty, fall back to the
# user-home default. Computed here (not in the SQLite store itself)
# so the path is visible alongside everything else and a future
# audit just has to grep ``config.py`` for "DEFAULT".
ANOTHER_CODER_SESSION_DB_PATH: str = (
    settings.another_coder_session_db_path
    or str(Path.home() / ".another_coder" / "sessions.db")
)

# Reaper tunables — exposed as constants so the singleton path
# (`build_default_reaper()`) can ``from config import ...``
# without instantiating Settings again. Tests that want to verify
# env-override behavior re-instantiate ``Settings()`` themselves.
ANOTHER_CODER_JOB_TTL_SECONDS: int = settings.another_coder_job_ttl_seconds
ANOTHER_CODER_SESSION_TTL_SECONDS: int = settings.another_coder_session_ttl_seconds
ANOTHER_CODER_REAPER_INTERVAL_SECONDS: int = settings.another_coder_reaper_interval_seconds

# Rate-limit constants — read by services/rate_limiter.py at
# module import. A consumer that wants to tighten / loosen at
# runtime should re-instantiate Settings() rather than mutating
# these (same pattern build_default_reaper uses).
ANOTHER_CODER_RATE_LIMIT_CHAT_STREAM: str = settings.another_coder_rate_limit_chat_stream
ANOTHER_CODER_RATE_LIMIT_INSTRUCT: str = settings.another_coder_rate_limit_instruct
ANOTHER_CODER_RATE_LIMIT_JOBS: str = settings.another_coder_rate_limit_jobs
ANOTHER_CODER_RATE_LIMIT_AUTH_VERIFY: str = settings.another_coder_rate_limit_auth_verify
ANOTHER_CODER_RATE_LIMIT_SESSIONS: str = settings.another_coder_rate_limit_sessions


# Claude Code transcript root. Resolution order mirrors Claude Code's
# own: explicit bridge override → CLAUDE_CONFIG_DIR/projects → the
# ~/.claude/projects default. Computed here so the whole app reads one
# frozen Path and a future audit greps config.py for "projects".
def _resolve_claude_projects_dir() -> Path:
    if settings.another_coder_claude_projects_dir.strip():
        return Path(settings.another_coder_claude_projects_dir).expanduser()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "projects"


CLAUDE_PROJECTS_DIR: Path = _resolve_claude_projects_dir()
