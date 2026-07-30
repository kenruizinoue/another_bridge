"""Text-to-speech proxy for the mobile client's high-quality voice.

``POST /tts`` forwards the text to the local OpenAI-compatible speech
endpoint (Kokoro, installed by VoiceMode, by default) and streams the
audio bytes back. The phone never talks to Kokoro directly — the bridge
is the one host it already reaches and authenticates against, and the
speech service stays bound to localhost.

Same auth gate and rate limit family as the session surface.
"""

from typing import Iterator

import requests
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import (
    ANOTHER_CODER_RATE_LIMIT_SESSIONS,
    ANOTHER_CODER_TTS_TIMEOUT_SECONDS,
    ANOTHER_CODER_TTS_URL,
    ANOTHER_CODER_TTS_VOICE,
)
from services.rate_limiter import limiter

log = structlog.get_logger()

router = APIRouter()

MAX_TTS_CHARS = 2_000  # spoken summaries are 1-2 sentences; hard-stop abuse


class TtsRequest(BaseModel):
    """Text to synthesize. ``voice`` falls back to the configured default;
    ``language`` is advisory (Kokoro voices are language-specific, so the
    client picks a voice per language and this field is informational)."""

    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)
    voice: str | None = None
    language: str | None = None


@router.post("/tts")
@limiter.limit(ANOTHER_CODER_RATE_LIMIT_SESSIONS)
def synthesize(request: Request, body: TtsRequest) -> StreamingResponse:
    """Proxy one synthesis call. 503 when no speech service is configured
    or reachable; upstream failures surface as 502 with a short detail."""
    if not ANOTHER_CODER_TTS_URL:
        raise HTTPException(status_code=503, detail="no TTS service configured")

    payload = {
        "model": "tts-1",
        "input": body.text,
        "voice": body.voice or ANOTHER_CODER_TTS_VOICE,
        "response_format": "mp3",
    }
    try:
        upstream = requests.post(
            ANOTHER_CODER_TTS_URL,
            json=payload,
            timeout=ANOTHER_CODER_TTS_TIMEOUT_SECONDS,
            stream=True,
        )
    except requests.RequestException as err:
        log.error("tts.unreachable", error=str(err))
        raise HTTPException(status_code=503, detail="TTS service unreachable")

    if upstream.status_code != 200:
        detail = upstream.text[:200]
        log.error("tts.upstream_error", status=upstream.status_code, detail=detail)
        raise HTTPException(status_code=502, detail=f"TTS upstream {upstream.status_code}: {detail}")

    def gen() -> Iterator[bytes]:
        for chunk in upstream.iter_content(chunk_size=16_384):
            if chunk:
                yield chunk

    media_type = upstream.headers.get("Content-Type", "audio/mpeg")
    return StreamingResponse(gen(), media_type=media_type)
