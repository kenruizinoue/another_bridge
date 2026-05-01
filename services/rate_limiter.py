"""Per-route rate limiting for the public bridge surfaces.

Without this, a leaked X-Coder-Key (committed to git, screenshot,
prompt-injected exfil, browser DevTools paste — anything) would
give an attacker an unbounded foothold. Even with the
``--dangerously-skip-permissions`` blast radius now bounded by
``WORKSPACE_ROOT`` (see routers/repos.validate_repo_path), a
flood of /chat/stream calls or /tools/instruct_implementation
calls could still drain Claude credits and pin the host CPU.

Implementation choice: ``slowapi`` is FastAPI-idiomatic, ships
in-process (no Redis dependency for our single-host bridge), and
has a small surface — one ``Limiter`` instance, one
``key_func``, decorators on the routes. The alternative
``fastapi-limiter`` requires Redis + asyncio context juggling
that's overkill for the use case.

Key strategy: prefer the X-Coder-Key over the remote IP. There's
only one key in production, so this effectively rate-limits the
entire deployment as a unit — which is exactly the right
granularity (one bridge, one operator, one sane cap). The IP
fallback exists so an unauth'd request to /health still gets
some kind of bucket if a future change exposes additional
unauth'd routes.

Tests: unit tests live in tests/test_rate_limiter.py. They
exercise the key_func + limit-spec parsing without spinning up
slowapi's full middleware (which is hard to mock cleanly).
Integration via TestClient is left to a single smoke test that
fires N+1 requests and asserts the Nth+1 returns 429.
"""

from __future__ import annotations

from typing import Optional

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request


# Header the bridge dispatcher sends; preferred over remote IP for
# rate-limit keying because only the operator has the key (and
# there's exactly one) — limiting per-key effectively limits per
# deployment, which is the right granularity here.
_KEY_HEADER = "X-Coder-Key"


def _coder_key_or_remote(request: Request) -> str:
    """slowapi key function. Returns the X-Coder-Key when present,
    or falls back to the remote address. The fallback is mostly
    defensive — auth-gated routes will have rejected the request
    before slowapi sees it if the header is missing."""
    key: Optional[str] = request.headers.get(_KEY_HEADER)
    if key:
        # Don't leak the key into bucket names visible in error
        # responses or logs. Hash to a short identifier — collisions
        # are fine because it's only a bucket key, not a security
        # boundary. (Built-in str.__hash__ is randomized per process
        # which is fine for in-memory state but means buckets reset
        # on restart — same property as the rest of slowapi's
        # in-memory backend.)
        return f"key:{abs(hash(key))}"
    return f"ip:{get_remote_address(request)}"


# Module-level singleton consumed by main.py on app construction.
# Initialized at import (using the captured config constants); a
# uvicorn restart picks up env changes naturally because import
# runs again.
limiter = Limiter(key_func=_coder_key_or_remote)


# Re-export RateLimitExceeded for handlers that want to register
# a custom 429 response.
__all__ = ["limiter", "RateLimitExceeded"]
