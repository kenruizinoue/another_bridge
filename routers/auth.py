"""Auth probe endpoint for the AnotherAgent Settings Connect flow.

The platform's Settings → Integrations → another_coder Connect dialog
calls this endpoint with the candidate `X-Coder-Key` before saving the
integration. A 200 response means the key matches `ANOTHER_CODER_API_KEY`
on this bridge; 401 means it doesn't; 503 means the bridge has no key
configured at all.

Why a dedicated probe instead of pinging /chat/stream:
  - /chat/stream is auth-gated AND spawns Claude Code as a side effect.
    Spawning a subprocess just to validate a key is wasteful.
  - /health is unauthed by design (uptime checks consume it without
    needing the bridge secret) so it can't validate the key.

The route is intentionally tiny — one auth-gated GET that returns a
constant payload. The auth dep does all the work.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from config import ANOTHER_CODER_RATE_LIMIT_AUTH_VERIFY
from services.rate_limiter import limiter

router = APIRouter()


@router.get("/auth/verify")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_AUTH_VERIFY)
def verify(request: Request) -> dict[str, bool]:
    return {"ok": True}
