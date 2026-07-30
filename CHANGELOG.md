# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-07-29

### Added

- Voice turns: `voice: true` on the resume endpoints (`/resume`,
  `/resume/stream`, `/resume/queue`) switches the run to
  `--output-format json --json-schema` with a `{reply, speech}` schema,
  so every conversation-mode turn is guaranteed to carry a short,
  code-free spoken summary. The stream variant runs blocking on a worker
  thread with SSE keepalives, then emits the full reply as one text
  chunk, a new `speech` event, and `done`. The transcript view unwraps
  the structured turn so clients render the reply, not JSON.
- `POST /tts` proxies a local OpenAI-compatible speech endpoint (Kokoro
  by default) for the mobile client's high-quality voice option. 503
  when unconfigured or unreachable, 502 on upstream errors, 2000-char
  cap. Configurable via `ANOTHER_CODER_TTS_URL`,
  `ANOTHER_CODER_TTS_VOICE`, and `ANOTHER_CODER_TTS_TIMEOUT_SECONDS`.

### Changed

- Every mobile-driven resume disallows the Mac's VoiceMode MCP tools, so
  a phone conversation can never speak or listen on the Mac.

## [0.2.0] - 2026-07-02

### Added

- File attachments on the resume endpoints (`/resume`, `/resume/stream`,
  `/resume/queue`): a `files` list of base64 payloads (PDF, txt, md, csv,
  json, code files, and other readable text formats). Files are saved
  under `~/.another_coder/attachments/<session_id>/` and the message
  gains a footer pointing Claude at the saved paths so it reads them
  with its own Read tool. Max 5 files, 20MB each, extension allowlist,
  filenames sanitized against path traversal.
- `ANOTHER_CODER_ATTACHMENTS_DIR` setting to relocate the attachments
  root.
- Queue previews distinguish images from files ("2 image(s), 1 file(s)").

## [0.1.0] - 2026-05-01

### Added

- Initial release: chat/stream bridge, planning + implementation
  webhooks, read-only session browsing API, mobile resume with SSE
  streaming, image attachments, server-side send queue, reconnection
  status endpoint.
