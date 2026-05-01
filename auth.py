"""Shared-secret header auth for the another_coder bridge.

Single header (`X-Coder-Key`) checked against `ANOTHER_CODER_API_KEY` from the
process environment via constant-time compare. Applied router-level on
endpoints that mutate state or trigger work (e.g. `/chat/stream`,
`/jobs/*`); `/health` stays unauthed so ngrok / uptime checks keep working
without leaking the key into monitoring configs.

Behavior intentionally tiny — JWT, OAuth, and per-tool keys are deliberately out
of scope for v1. Same key is configured server-side here and client-side in the
calling AnotherAgent agent's `llmConfig.coderApiKey`.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


_CODER_KEY_HEADER_NAME = "X-Coder-Key"


def _expected_key() -> str:
    # Read on every call (not cached at import time) so a deploy that
    # rotates the key picks it up without a process restart.
    return os.environ.get("ANOTHER_CODER_API_KEY", "")


def verify_api_key(
    x_coder_key: str | None = Header(default=None, alias=_CODER_KEY_HEADER_NAME),
) -> None:
    """FastAPI dependency. Raises 401 on missing / mismatched key.

    Returns None on success — callers don't need the value, just the
    side effect of the check. The header name is intentionally distinct
    from the platform's own X-API-Key (which the AnotherAgent backend
    uses for tenant scoping) so a single request that hops platform →
    bridge can carry both without collision and without ambiguity in
    middleware logs.
    """
    expected = _expected_key()
    if not expected:
        # Hard-fail rather than silently allow-all: an empty env var on
        # a public-facing host would mean every request is treated as
        # authed, which is the exact misconfiguration this dep is meant
        # to prevent. /health is excluded from this dep so uptime checks
        # still work.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANOTHER_CODER_API_KEY is not configured on this bridge",
        )
    if not x_coder_key or not hmac.compare_digest(x_coder_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Coder-Key",
        )
