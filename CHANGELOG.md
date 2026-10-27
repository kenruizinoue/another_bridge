# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A phone message to a session that is working in a terminal tab waits in
  the queue instead of starting a second copy of the session. "Working"
  means the tab is busy, or its own lines or its agents changed in the
  last 120 s (`ANOTHER_BRIDGE_TERMINAL_ACTIVE_SECONDS`). `/resume/stream`
  answers with a `queued` event and `done`; `/resume` answers 409.
- At most 2 phone turns run at once across sessions
  (`ANOTHER_BRIDGE_MAX_RUNNING_TURNS`); more wait in the queue.
- "Send anyway": `"force": true` on a send, or
  `POST /sessions/{id}/resume/queue/release` for what is already queued.
- `terminal` on `/sessions` cards and `/resume/status`, and `held` on
  `/resume/status`.
- `scripts/terminal_catch_up.py`, a Claude Code `UserPromptSubmit` hook
  that catches a terminal tab up on phone turns it hasn't seen, like
  /sync-mobile, once per turn.

- `GET /sessions/{id}/resume/attach`: a phone that left the chat (or was
  frozen by iOS) watches the rest of a running turn stream in live. Every
  resume path now publishes its `text` and `tool` events to an in-memory
  live run, whether or not a phone is connected. Matching client change
  in another_bridge_mobile 1.5.0.

- Read-only Files for the phone: `GET /files/roots`, `GET /files/list`,
  `GET /files/raw` (Range requests, so videos stream) and
  `GET /files/locate`. Only the folders named in
  `ANOTHER_BRIDGE_FILE_ROOTS` are reachable, by root name and relative
  path. Hidden and key files are never listed or served. Matching client
  change in another_bridge_mobile 1.4.0.
- `GuardMiddleware` checks the key before any request body is read and
  caps bodies at 40 MB on the resume routes and 64 KB elsewhere (413).
  Ten failed key checks in a minute from one address get 429.
- A separate phone key, `ANOTHER_BRIDGE_PHONE_KEY`. The phone routes no
  longer accept `ANOTHER_CODER_API_KEY`, which now opens only the
  AnotherAgent routes. `/auth/verify` takes either key.

### Changed

- The fallback model for phone turns (`ANOTHER_CODER_RESUME_MODEL`) is now
  `claude-opus-5-5` instead of `claude-opus-4-8`. It applies only to
  sessions with no model recorded; others keep their last model.
- Queued text turns (`/resume/queue`) stream like a live send, so a phone
  can attach to them. They are no longer cut off after 300 s.
- The server refuses to start when a key is shorter than 32 characters,
  when both keys are the same, or when a file root is invalid.
- A key header with non-ASCII characters gets 401 instead of 500.
- `/docs`, `/redoc`, `/openapi.json` and unknown paths now need the
  AnotherAgent key.
- Images on the resume routes are capped at about 10 MB each (422).

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
