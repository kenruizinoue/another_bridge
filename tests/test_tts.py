"""``POST /tts`` — the Kokoro proxy for mobile voice conversations:
happy-path proxying (payload shape, audio bytes, content type), the
configured default voice, upstream failures as 502, unreachable or
unconfigured service as 503, and input validation."""

from __future__ import annotations

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import tts as tts_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(tts_router.router)
    return TestClient(app)


class _FakeUpstream:
    def __init__(self, status_code=200, content=b"MP3BYTES", content_type="audio/mpeg"):
        self.status_code = status_code
        self._content = content
        self.headers = {"Content-Type": content_type}
        self.text = content.decode("latin-1")

    def iter_content(self, chunk_size):
        yield self._content


def test_proxies_text_to_the_speech_service(client, monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout, stream):
        captured["url"] = url
        captured["json"] = json
        return _FakeUpstream()

    monkeypatch.setattr(tts_router.requests, "post", fake_post)

    resp = client.post("/tts", json={"text": "Done, tests are green."})
    assert resp.status_code == 200
    assert resp.content == b"MP3BYTES"
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert captured["json"]["input"] == "Done, tests are green."
    assert captured["json"]["voice"]  # configured default applied


def test_explicit_voice_overrides_the_default(client, monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout, stream):
        captured["json"] = json
        return _FakeUpstream()

    monkeypatch.setattr(tts_router.requests, "post", fake_post)
    client.post("/tts", json={"text": "hola", "voice": "ef_dora", "language": "es-ES"})
    assert captured["json"]["voice"] == "ef_dora"


def test_upstream_error_becomes_502(client, monkeypatch):
    monkeypatch.setattr(
        tts_router.requests, "post",
        lambda url, json, timeout, stream: _FakeUpstream(status_code=500, content=b"kokoro down"),
    )
    resp = client.post("/tts", json={"text": "hi"})
    assert resp.status_code == 502
    assert "500" in resp.json()["detail"]


def test_unreachable_service_becomes_503(client, monkeypatch):
    def fake_post(url, json, timeout, stream):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(tts_router.requests, "post", fake_post)
    resp = client.post("/tts", json={"text": "hi"})
    assert resp.status_code == 503


def test_unconfigured_service_becomes_503(client, monkeypatch):
    monkeypatch.setattr(tts_router, "ANOTHER_CODER_TTS_URL", "")
    resp = client.post("/tts", json={"text": "hi"})
    assert resp.status_code == 503


def test_empty_and_oversize_text_rejected(client):
    assert client.post("/tts", json={"text": ""}).status_code == 422
    assert client.post("/tts", json={"text": "x" * 2001}).status_code == 422
