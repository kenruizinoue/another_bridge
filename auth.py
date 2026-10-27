"""Shared-secret header auth for the another_bridge service.

Two keys, one per caller, both sent in the `X-Coder-Key` header:

- the phone key, `ANOTHER_BRIDGE_PHONE_KEY`, for `/sessions*`, `/tts`,
  `/transcribe` and `/files*` (the another_bridge_mobile app);
- the AnotherAgent key, `ANOTHER_CODER_API_KEY`, for the webhook routes
  (`/chat/stream`, `/jobs/*`, `/tools/*`).

`/auth/verify` accepts either key, and `/health` stays open. The first
gate is `services.guard.GuardMiddleware`, which checks the key from the
headers before any body is read; the dependencies below repeat the check
per router as a second layer.

Keys are compared as bytes in constant time, so a header with non-ASCII
characters is a plain mismatch. Keys are read from the environment on
each call, but `validate_keys` only runs at boot, so restart the server
after rotating a key.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status

_CODER_KEY_HEADER_NAME = "X-Coder-Key"

PHONE = "phone"
AGENT = "agent"

KEY_ENV = {
    PHONE: "ANOTHER_BRIDGE_PHONE_KEY",
    AGENT: "ANOTHER_CODER_API_KEY",
}

MIN_KEY_LENGTH = 32


class KeyConfigError(RuntimeError):
    """The configured keys are unsafe. Raised at boot, never with a key value."""


def expected_key(surface: str) -> str:
    return os.environ.get(KEY_ENV[surface], "")


def key_matches(surface: str, provided: str | bytes | None) -> bool:
    """True when `provided` equals the configured key for `surface`.

    An unset key never matches, so a surface without a key stays closed.
    """
    expected = expected_key(surface)
    if not expected or not provided:
        return False
    if isinstance(provided, str):
        try:
            provided = provided.encode("ascii")
        except UnicodeEncodeError:
            return False
    return hmac.compare_digest(provided, expected.encode("utf-8"))


def validate_keys() -> None:
    """Boot check: at least one key, each long enough, and never shared."""
    values = {surface: expected_key(surface) for surface in KEY_ENV}
    if not any(values.values()):
        raise KeyConfigError(
            f"set {KEY_ENV[PHONE]} and/or {KEY_ENV[AGENT]} before starting the bridge"
        )
    for surface, value in values.items():
        if value and len(value) < MIN_KEY_LENGTH:
            raise KeyConfigError(
                f"{KEY_ENV[surface]} must be at least {MIN_KEY_LENGTH} characters"
            )
    if values[PHONE] and values[PHONE] == values[AGENT]:
        raise KeyConfigError(f"{KEY_ENV[PHONE]} and {KEY_ENV[AGENT]} must differ")


def _check(surfaces: tuple[str, ...], provided: str | None) -> None:
    if not any(expected_key(s) for s in surfaces):
        # Hard-fail rather than silently allow-all: an empty env var on a
        # public host would otherwise treat every request as authed.
        names = " or ".join(KEY_ENV[s] for s in surfaces)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{names} is not configured on this bridge",
        )
    if not any(key_matches(s, provided) for s in surfaces):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Coder-Key",
        )


def verify_api_key(
    x_coder_key: str | None = Header(default=None, alias=_CODER_KEY_HEADER_NAME),
) -> None:
    """Dependency for the AnotherAgent routes. 401 on a missing or wrong
    key, 503 when `ANOTHER_CODER_API_KEY` is unset."""
    _check((AGENT,), x_coder_key)


def verify_phone_key(
    x_coder_key: str | None = Header(default=None, alias=_CODER_KEY_HEADER_NAME),
) -> None:
    """Dependency for the phone routes. 401 on a missing or wrong key, 503
    when `ANOTHER_BRIDGE_PHONE_KEY` is unset."""
    _check((PHONE,), x_coder_key)


def verify_any_key(
    x_coder_key: str | None = Header(default=None, alias=_CODER_KEY_HEADER_NAME),
) -> None:
    """Dependency for `/auth/verify`, which both callers use."""
    _check((PHONE, AGENT), x_coder_key)
