"""ASGI guard that runs before every route.

The bridge is reachable over a public ngrok URL, so the cheap attacks are
stopped here, from the request line and headers alone:

1. Throttle. An address with 10 failed key checks in the last minute gets
   429 on everything but `/health` until the minute passes.
2. Key check. Each path belongs to a surface (see `surface_for`), and the
   request needs that surface's key (see `auth.py`). A rejected request
   never has its body read.
3. Size caps. A Content-Length over the route's cap gets 413 at once; a
   body without one is counted while it streams and cut off at the cap.

Guard responses use the structured error shape `{"detail", "code"}`.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Callable

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import auth

OPEN = "open"
ANY = "any"

# First path segment to surface. Everything else, including /docs and
# unknown paths, is the AnotherAgent surface.
_PHONE_SEGMENTS = frozenset({"sessions", "tts", "transcribe", "files"})

LARGE_BODY_BYTES = 40 * 1024 * 1024
SMALL_BODY_BYTES = 64 * 1024

MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 60.0
_MAX_TRACKED_ADDRESSES = 10_000

_LOCALHOST = frozenset({"127.0.0.1", "::1", "localhost"})


def surface_for(path: str) -> str:
    if path == "/health":
        return OPEN
    if path == "/auth/verify":
        return ANY
    segment = path.lstrip("/").split("/", 1)[0]
    if segment in _PHONE_SEGMENTS:
        return auth.PHONE
    return auth.AGENT


def _is_large_body_route(path: str) -> bool:
    # Resume routes carry base64 images and files; /transcribe carries audio.
    if path == "/transcribe":
        return True
    parts = path.strip("/").split("/")
    return len(parts) >= 3 and parts[0] == "sessions" and parts[2] == "resume"


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value
    return None


def client_ip(scope: Scope) -> str:
    """The caller's address. ngrok connects from localhost and appends the
    real client to X-Forwarded-For, so the last entry is trusted only then."""
    client = scope.get("client")
    host = client[0] if client else None
    if host in _LOCALHOST:
        forwarded = _header(scope, b"x-forwarded-for")
        if forwarded:
            last = forwarded.decode("latin-1").split(",")[-1].strip()
            if last:
                return last
    return host or "unknown"


class _BodyTooLarge(HTTPException):
    """Raised from `receive` once a streamed body crosses its cap. FastAPI
    re-raises HTTPException while parsing a body, so this becomes a 413."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body is too large")


class GuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        large_body_bytes: int = LARGE_BODY_BYTES,
        small_body_bytes: int = SMALL_BODY_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.large_body_bytes = large_body_bytes
        self.small_body_bytes = small_body_bytes
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        surface = surface_for(path)
        if surface == OPEN:
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope)
        if self._throttled(ip):
            await _reply(send, 429, "too many failed attempts, wait a minute", "too_many_failed_attempts")
            return

        surfaces = (auth.PHONE, auth.AGENT) if surface == ANY else (surface,)
        if not any(auth.expected_key(s) for s in surfaces):
            await _reply(send, 503, "this route is not configured on the bridge", "not_configured")
            return
        provided = _header(scope, b"x-coder-key")
        if not any(auth.key_matches(s, provided) for s in surfaces):
            self._record_failure(ip)
            await _reply(send, 401, "invalid or missing X-Coder-Key", "unauthorized")
            return

        cap = self.large_body_bytes if _is_large_body_route(path) else self.small_body_bytes
        length = _header(scope, b"content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                await _reply(send, 400, "invalid Content-Length", "bad_request")
                return
            if declared > cap:
                await _reply(send, 413, "request body is too large", "body_too_large")
                return

        received = 0
        started = False
        too_large = False

        async def capped_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if too_large and not started:
                # Swap the app's own 413 for the structured one.
                if message["type"] == "http.response.start":
                    started = True
                    await _reply(send, 413, "request body is too large", "body_too_large")
                return
            if too_large:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            if not started:
                started = True
                await _reply(send, 413, "request body is too large", "body_too_large")

    def _throttled(self, ip: str) -> bool:
        stamps = self._failures.get(ip)
        if not stamps:
            return False
        cutoff = self.clock() - FAILURE_WINDOW_SECONDS
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if not stamps:
            del self._failures[ip]
            return False
        return len(stamps) >= MAX_FAILURES

    def _record_failure(self, ip: str) -> None:
        if ip not in self._failures and len(self._failures) >= _MAX_TRACKED_ADDRESSES:
            # Drop the oldest tracked address so a spray of addresses cannot
            # grow this map without bound.
            self._failures.pop(next(iter(self._failures)))
        stamps = self._failures.setdefault(ip, deque())
        stamps.append(self.clock())
        while len(stamps) > MAX_FAILURES:
            stamps.popleft()


async def _reply(send: Send, status: int, detail: str, code: str) -> None:
    body = json.dumps({"detail": detail, "code": code}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
