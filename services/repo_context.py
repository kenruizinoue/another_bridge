"""Build the platform's __context__.selected_repo payload from a resolved
repo path. Lifted out of routers/planning.py so routers/implementation.py
no longer has to reach across into a sibling router for it.

Re-exported from routers/planning as `_build_selected_repo_context` to
preserve the patch target used by tests/test_planning_payload.py.
"""

from __future__ import annotations

import os
from typing import Any

from services.git_service import _git_origin_url, _parse_owner_repo


def build_selected_repo_context(repo_path: str) -> dict[str, Any]:
    """Shape the platform's __context__.selected_repo payload from a
    resolved repo path. The platform persists this on the assistant
    message and surfaces it in the next turn's [Conversation context]
    block so the Developer Agent can read repo_path structurally instead
    of parsing markdown out of conversation history.

    owner_repo is best-effort — when the origin remote isn't a GitHub URL
    (or no origin is configured), the field is omitted. The path + name
    are always present.
    """
    out: dict[str, Any] = {
        "path": repo_path,
        "name": os.path.basename(repo_path.rstrip("/")),
    }
    origin_url = _git_origin_url(repo_path)
    if origin_url:
        owner_repo = _parse_owner_repo(origin_url)
        if owner_repo:
            out["owner_repo"] = owner_repo
    return out
