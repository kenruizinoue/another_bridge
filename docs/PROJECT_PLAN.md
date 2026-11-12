# another_bridge: project plan

## Next up (order agreed with Ken, 2026-11-08)

1. Ticket 31: Tailscale, ngrok off (with app ticket 33). Starts when Ken
   has Tailscale installed and signed in on the Mac and the iPhone.
2. Keys out of the app: app ticket 3 (Connect screen, key in the Keychain)
   and app ticket 4 with ticket 14 here (OpenAI through the bridge).
3. Face ID lock: app ticket 28.
4. The rest of the security pass: tickets 1, 4 to 13, 15 to 18.
5. Later: the AnotherTask link ideas (`docs/ANOTHERTASK_LINK_IDEA.txt`)
   and the items under "Optional, not scheduled".

The sections below started as the v0.4.0 security and quality pass
(2026-10-23) and grew with every phase since.

## Goal

Make the bridge safe to run behind its public ngrok URL, while the phone keeps
working the same way. Today one shared key gives full control of the Mac, the
server reads whole request bodies before checking that key, and a user message
can turn into a command line option. After this pass:

- Requests without a valid key are rejected before their body is read, and
  every request has a size cap.
- The phone and AnotherAgent each have their own key, and the AnotherAgent
  routes are off unless switched on.
- Every `claude` run gets its message safely, has a deadline, runs inside the
  home folder, and counts against a global limit.
- The phone's OpenAI calls go through the bridge, so the OpenAI key stays on
  the Mac.
- The docs describe what the code really does.

## Decisions (confirmed by Ken, 2026-10-23)

1. Keep ngrok for now. Tailscale is an optional ticket for later.
   (Replaced by decision 13 on 2026-11-07.)
2. The phone's key is entered once in the app and stored in the iPhone
   Keychain (client side, see the mobile plan), then the key is rotated.
3. Separate keys for the phone and for AnotherAgent.
4. AnotherAgent routes are off by default and switched on by a setting.
5. A resume runs only in the home folder or below.
6. Barge-in off by default and a long press for hands-free mode (client side).
7. E2E is planned but only built after Ken approves it.
8. No changes to the AnotherAgent repos.
9. Worth adding on top (Ken: "improve the things worth adding"): index
   session ids once instead of rescanning, and dependency updates.
10. Files from the phone (Ken, 2026-10-24: "add a new ui button like
    folders besides the refresh button so we can see the specified
    folder"). Read only, `~/agent_outputs` first. The server owns the list
    of shared folders in `.env`, so another folder is one more entry and a
    restart, with no app change. Tickets 2 and 3 go first because this
    serves files over the public URL.
11. Phone sync (Ken, 2026-10-26: "i get this when going outside the chat
    after asking a thing ... audit how the mac syncing is ... to improve
    this experience"). Coming back to a running turn shows it streaming
    live instead of "reconnecting" with 30 s polls.
12. One copy of a session at a time (Ken, 2026-10-27: "it got duplicated
    agents ... propose a fix", then "implement it, do not install until i
    say so"). A phone message to a session that is working in a terminal
    tab waits in the bridge's queue instead of starting a second copy. At
    most 3 phone turns run at once (2 at first, raised the same day). "Send anyway" skips both. A hook
    catches terminal tabs up on phone turns, and every session only
    messages its own agents (playbook rule).

13. Tailscale instead of ngrok (Ken, 2026-11-07, "create the ticket"):
    only Ken's own devices can reach the bridge (ticket 31).
14. Always on (Ken, 2026-11-07, "do the launchd approach"): the server and
    ngrok are launchd services that start at login, turned off with
    `scripts/bridge_service.py off` (ticket 32). No desktop app for now
    (2026-11-08): the private web chat can carry a status page later.
15. Security order (Ken, 2026-11-08): Tailscale first, then the keys out
    of the app, then the Face ID lock (see "Next up").
16. Sessions started from the phone show in Claude Code's /resume like the
    others (Ken, 2026-11-11, ticket 33).

## Contract with the phone app (another_bridge_mobile 1.4.0)

- Auth header stays `X-Coder-Key`, with the phone key as its value.
- Errors: HTTP error bodies are `{"detail": "<safe message>", "code":
  "<snake_case>"}` (old clients keep reading `detail`). SSE `error` events
  carry `{"message": "...", "code": "..."}`. A busy session on the stream
  route returns HTTP 409 before the stream starts.
- `POST /transcribe` with JSON `{"audio": "<base64>", "mime_type":
  "audio/m4a", "language": "en"}` returns `{"text": "..."}`. 25 MB audio cap.
  503 when no OpenAI key is set.
- `POST /tts` gains `"engine": "kokoro" | "openai"` (default `kokoro`). The
  OpenAI engine returns `audio/mpeg`.
- Too many requests return 429; oversize bodies return 413.
- Files (ticket 19), all `GET` with the phone key:
  - `/files/roots` returns `{"roots": [{"name", "label"}]}`, never a path.
  - `/files/list?root=<name>&path=<relative>` returns `{"root", "path",
    "entries": [{"name", "path", "kind", "size", "modified", "preview"?}],
    "truncated"}`. `kind` is `dir`, `video`, `image`, `audio`, `pdf`,
    `text` or `other`. Folders first, then newest first. A folder's
    `preview` is its cover image, when it has one.
  - `/files/raw?root=<name>&path=<relative>` serves the file inline with
    Range support. Text of any kind is `text/plain`.
  - `/files/locate?path=<absolute or ~/ path>` returns `{"root", "path",
    "kind"}` for a path shown in a chat.
  - Errors: 403 `not_shared`, 404 `not_found`.
- Re-attach (ticket 20): `GET /sessions/{id}/resume/attach` is an SSE
  stream with the same `text` and `tool` events as `/resume/stream`,
  keepalive comments every 10 s, and `done` when the turn ends (at once
  when nothing runs). It replays only the message being written now, so
  the phone fetches `/messages` first. Voice and blocking turns only send
  `done`.
- Terminal copies and the turn cap (ticket 21, app 1.6.0):
  - `/sessions` cards and `/resume/status` carry `terminal`: `null`, or
    `{"open", "busy", "active", "agents"}` for a live terminal tab holding
    the session. `active` means it is working now.
  - `/resume/status` carries `held`: `"terminal"`, `"limit"` or `null`, why
    queued messages wait, and `held_by`: the `{"session_id", "title"}` of the
    sessions holding the slots when it is `"limit"`.
  - `/resume/stream` (text and voice) answers a held send with an SSE
    `queued` event, `{"reason", "queued", "message"}`, then `done`. The
    queue runs it later. `/resume` answers 409.
  - Request bodies take `"force": true` to skip both checks.
  - `POST /sessions/{id}/resume/queue/release` releases the queued
    messages ("Send anyway") and returns `{"released": n}`.
- New sessions and Open on Mac (ticket 25, app 1.8.0):
  - `POST /sessions/new/stream` takes `{"message", "images", "files",
    "name", "force"}` and streams SSE: `session` `{"session_id", "cwd",
    "name"}` once the transcript exists, then `text`, `tool` and `done`
    like `/resume/stream`. The turn cap answers `error` with `"reason":
    "limit"`. The phone may close the stream after `session`; the turn
    keeps running and `/resume/attach` follows it.
  - `POST /sessions/{id}/open-on-mac` returns `{"ok", "status", "app"}`:
    `status` is `opened`, `waiting` or `already_open`, `app` is `iterm`
    or `terminal`. 404, 422 (no folder) and 502 (osascript failed, with
    its message in `detail`).
  - `/resume/status` carries `opening_on_mac`.
- Card fields (tickets 22 and 23): `last_activity` is the last real
  message, `title` the newest custom or AI title. Same names and types.
- A running turn and the queue (ticket 26, app 1.9.0):
  - `POST /sessions/{id}/resume/inject` takes the `/resume` body and
    returns `{"status": "sent_mid_turn", "id"}`, or 409.
  - Live streams (`/resume/stream`, `/resume/attach`, new sessions) send
    `steer` `{"id", "state": "sent" | "read", "text"?}`.
  - `POST /sessions/{id}/resume/stop` returns `{"stopped", "paused"}`;
    `POST /sessions/{id}/resume/queue/run` resumes a paused queue.
  - `/resume/queue` returns the new item's `id`. `DELETE` and `PATCH
    /sessions/{id}/resume/queue/{item}` (`{"message"}`), `POST
    .../{item}/restore`, `POST .../{item}/send-now` returns `{"status":
    "sent_mid_turn" | "next" | "starting"}`, `POST
    .../queue/send-all-now` returns `{"sent": [{"id", "status"}]}`.
  - `/resume/status` adds `queue_items` `[{"id", "preview", "text"}]`,
    `can_inject`, `steers` `[{"id", "text", "state"}]`, `paused`.

## Status

| Ticket | Name | Status |
|---|---|---|
| 1 | CI hygiene and lint | Todo |
| 2 | Key check before the body, size caps | Done (2026-10-24) |
| 3 | Separate keys, strict key checks, failed-auth throttle | Done (2026-10-24) |
| 4 | Smaller public surface | Todo |
| 5 | Phase 1 audit | Todo |
| 6 | Move resume logic into a service | Todo |
| 7 | Messages always go through stdin | Todo |
| 8 | Deadline and clean process handling | Todo |
| 9 | Global limit on claude runs | Todo |
| 10 | Resume only inside the home folder | Todo |
| 11 | Attachment hygiene | Todo |
| 12 | Structured errors, no leaks in errors or logs | Todo |
| 13 | Phase 2 audit | Todo |
| 14 | OpenAI proxy for the phone | Todo |
| 15 | Index session ids once | Todo |
| 16 | Dependency updates | Todo |
| 17 | One version source, docs rewrite | Todo |
| 18 | Phase 3 audit | Todo |
| 19 | Read-only files for the phone | Done (2026-10-24) |
| 20 | Re-attach to a running turn | Done (2026-10-26) |
| 21 | One copy of a session at a time | Done, not deployed (2026-10-27) |
| 22 | Card time from the last real message | Done (2026-11-05) |
| 23 | Card title from the newest name | Done (2026-11-05) |
| 24 | Phone turns run long work in the foreground | Done (2026-11-05) |
| 25 | New session from the phone, Open on Mac | Done (2026-11-05) |
| 26 | Talk to a running turn, queue actions | Done (2026-11-05) |
| 27 | End a finished turn kept alive in the background | Done (2026-11-05) |
| 28 | Delete a session from the phone | Done (2026-11-05) |
| 29 | What runs on the Mac, and stop a command | Done (2026-11-05) |
| 30 | Stop a session wherever it runs | Done (2026-11-05) |
| 31 | Private network with Tailscale, ngrok off | Todo |
| 32 | Start at login with launchd | Done (2026-11-07) |
| 33 | Phone-made sessions in /resume | Done (2026-11-11) |

## Phase 1: stop the cheap attacks

### Ticket 1 - CI hygiene and lint

Objective:

- CI runs on every push and PR to `dev`, follows the workflow hygiene rules,
  and lints the code.

TDD work:

- No product code. Add `ruff` to the dev extra with a config in
  `pyproject.toml`, fix what it reports, and add a lint step.

Edge cases:

- Docs-only changes still skip the test job.

Acceptance criteria:

- `.github/workflows/test.yml` has `permissions: contents: read`, a
  concurrency group with cancel-in-progress, `timeout-minutes`, push and PR
  triggers on `dev`, pip caching, `ruff check .` and `pytest`.
- `ruff check .` and `pytest` pass locally.

### Ticket 2 - Key check before the body, size caps

Objective:

- An ASGI middleware checks the key on protected routes before any body is
  read, and caps request sizes.

TDD work:

- Failing tests: an unauthenticated POST with a large streamed body gets 401
  and the body stream is never consumed; an authenticated body over the cap
  gets 413; a body with no Content-Length is cut off at the cap while
  streaming; an image attachment over its own cap gets 422.
- Then the middleware and the caps.

Edge cases:

- Chunked bodies with no Content-Length.
- Caps: 40 MB on the resume routes and the coming `/transcribe`, 64 KB on
  everything else, set in config.
- `/health` stays open.

Acceptance criteria:

- Memory stays flat when a 200 MB unauthenticated POST is sent (checked by
  hand once, the tests check the body is never read).
- All existing tests pass.

### Ticket 3 - Separate keys, strict key checks, failed-auth throttle

Objective:

- A phone key and an AnotherAgent key, each limited to its own routes, checked
  safely, and repeated failures slowed down.

TDD work:

- Failing tests: the phone key works on `/sessions*`, `/tts`, `/transcribe`
  and `/auth/verify` and gets 401 on the AnotherAgent routes; the agent key is
  the reverse; a non-ASCII key header gets 401, not 500; boot fails when a key
  is shorter than 32 characters; after 10 failed attempts in a minute an IP
  gets 429.
- Then the code.

Edge cases:

- Only one key configured: the other surface stays closed.
- The same value for both keys is rejected at boot.
- ngrok forwards the client IP in `X-Forwarded-For`; trust it only from
  localhost.

Acceptance criteria:

- New setting `ANOTHER_BRIDGE_PHONE_KEY`; `ANOTHER_CODER_API_KEY` stays the
  AnotherAgent key. `.env.example` documents both.
- The rotation comment in `auth.py` says a restart is needed.

### Ticket 4 - Smaller public surface

Objective:

- Only what the phone needs is reachable by default.

TDD work:

- Failing tests: with defaults, the AnotherAgent routes and `/docs`, `/redoc`,
  `/openapi.json` return 404; with `ANOTHER_BRIDGE_ENABLE_ANOTHERAGENT=true`
  and `ANOTHER_BRIDGE_ENABLE_DOCS=true` they come back; unauthenticated
  `/health` returns only `{"ok": true}`, and with a key it also returns
  versions and probes.
- Then the code.

Edge cases:

- The claude probe still runs at boot, so a broken install is visible in the
  log.

Acceptance criteria:

- Defaults match decision 4. The flags are in `.env.example`.

### Ticket 5 - Phase 1 audit

Objective:

- Close phase 1 with the edge cases covered.

TDD work:

- List the inputs, states and error paths not yet tested in tickets 1 to 4,
  add tests for the real gaps, fix what they expose.

Edge cases:

- Unknown routes, HEAD and OPTIONS requests, very long headers.

Acceptance criteria:

- All checks green locally and in CI. Phase marked done here.

## Phase 2: safe claude runs

### Ticket 6 - Move resume logic into a service

Objective:

- `routers/sessions.py` only validates, calls a service and translates the
  result. The queue worker, locks, process handling and argument building move
  to `services/resume.py`, with one argument builder instead of four.

TDD work:

- Behavior stays the same, so the existing tests are the safety net. Add unit
  tests for the argument builder and the reply parsing in the new service.

Edge cases:

- Voice turns, queued turns, image and file attachments all keep working.

Acceptance criteria:

- No route handler builds `claude` arguments or touches processes.
- All tests pass with no behavior change.

### Ticket 7 - Messages always go through stdin

Objective:

- A user message can never become a command line option.

TDD work:

- Failing tests: for every resume path (blocking, stream, queue, voice) and
  `/chat/stream`, a message like `--version` or `- fix the list` never
  appears in argv and arrives on stdin as the user turn.
- Then send every message through `--input-format stream-json` on stdin,
  like images already are.

Edge cases:

- Empty text with only images or files.
- Very long and multibyte messages.

Acceptance criteria:

- Checked by hand once: sending `--help` from the phone gets a normal reply.

### Ticket 8 - Deadline and clean process handling

Objective:

- A stuck run can never lock a session.

TDD work:

- Failing tests with a fake process: a run over the deadline is killed with
  its process group and the client gets an error event; stderr is drained so
  a chatty process cannot block; a non-zero exit sends an error, not `done`;
  resume jobs end as done or failed; the queue worker stops when the queue is
  empty.
- Then the code.

Edge cases:

- The client disconnects mid-stream.
- The process exits before writing anything.

Acceptance criteria:

- After a forced timeout, the next resume on the same session works.

### Ticket 9 - Global limit on claude runs

Objective:

- At most 3 `claude` processes at once, and the queue cannot grow without
  bound.

TDD work:

- Failing tests: a 4th concurrent run waits or gets 429 by config; a queue
  over its cap gets 429; the resume routes share one rate limit of 10 per
  minute per key; `/health` still answers while runs are busy.
- Then a semaphore in the resume service and a shared limit.

Edge cases:

- A run that fails to start releases its slot.

Acceptance criteria:

- Limits are settings with these defaults and are in `.env.example`.

### Ticket 10 - Resume only inside the home folder

Objective:

- A resume runs only in a folder that exists inside the home folder.

TDD work:

- Failing tests: a transcript whose cwd is outside home, missing, or a
  symlink out of home gets 403; home itself and any folder below it work.
- Then the check, using the resolved real path.

Edge cases:

- A cwd with spaces or non-ASCII characters.

Acceptance criteria:

- The home folder is the default bound; a setting can narrow it.

### Ticket 11 - Attachment hygiene

Objective:

- Attachments are saved only for a run that starts, readable only by the
  owner, and cleaned up.

TDD work:

- Failing tests: a busy session (409) saves nothing; saved files are mode
  0600 in a 0700 folder; the reaper removes attachment folders older than the
  session TTL.
- Then the code.

Edge cases:

- Two attachments with the same name.

Acceptance criteria:

- No attachment is written before the session lock is held.

### Ticket 12 - Structured errors, no leaks in errors or logs

Objective:

- Errors are `{code, message}` with no internal details, and logs never hold
  message text or raw output.

TDD work:

- Failing tests: a failed run returns a generic code with no stderr text;
  `/tts` upstream errors return a generic message; a busy session on the
  stream route gets HTTP 409 before the stream starts; no log line contains
  the message body or raw claude output.
- Then the code. Also bind the Kokoro example in `docker-compose.yml` to
  127.0.0.1.

Edge cases:

- The phone app still shows a readable message for each code (matching
  client change in the mobile plan).

Acceptance criteria:

- The error codes are listed in `docs/CURL_EXAMPLES.md`.

### Ticket 13 - Phase 2 audit

Objective:

- Close phase 2 with the edge cases covered.

TDD work:

- List what tickets 6 to 12 left untested, add the tests, fix what they
  expose.

Edge cases:

- Combined cases: a timeout while at the run limit, a queued turn on a
  session whose cwd became invalid.

Acceptance criteria:

- All checks green locally and in CI, plus one real resume from the phone.

## Phase 3: phone support and docs

### Ticket 14 - OpenAI proxy for the phone

Objective:

- The phone transcribes and speaks with OpenAI through the bridge, so the
  OpenAI key lives only in the bridge's `.env`.

TDD work:

- Failing tests with OpenAI mocked: `POST /transcribe` takes audio (size cap,
  allowed types) and a language and returns text; `/tts` with
  `engine=openai` returns audio; both return 503 when no key is set and a
  generic 502 on upstream errors; both need the phone key.
- Then the routes, thin, over a small OpenAI service.

Edge cases:

- Empty audio, unsupported type, text over the 2,000 character cap.

Acceptance criteria:

- `OPENAI_API_KEY` documented in `.env.example`. Contract noted in the
  CHANGELOG for the mobile app.

### Ticket 15 - Index session ids once

Objective:

- Session lookups stop rescanning every transcript file.

TDD work:

- Failing tests: a lookup by id reads at most one transcript after the first
  scan; a new transcript file is found without a restart; subagent
  transcripts are skipped. Then an id to path index kept fresh by folder
  modification times.

Edge cases:

- A transcript deleted while indexed.

Acceptance criteria:

- `GET /sessions` and resume lookups are noticeably faster on Ken's 160 plus
  transcripts (timed once by hand).

### Ticket 16 - Dependency updates

Objective:

- Current FastAPI, Starlette and uvicorn, and no deprecation warnings.

TDD work:

- Raise the minimum versions, add upper bounds on the next major, fix the
  warnings the suite prints (slowapi coroutine check, TestClient httpx,
  unclosed sqlite connections). CI runs on Python 3.11 and 3.14.

Edge cases:

- The Docker image still builds.

Acceptance criteria:

- `pytest -W error::DeprecationWarning` passes or each remaining warning is
  from a third party and listed in the CHANGELOG.

### Ticket 17 - One version source, docs rewrite

Objective:

- `/health` reports the real version, and the docs match the code.

TDD work:

- Failing test: the version in `/health` equals the one in `pyproject.toml`.
- Then read it from one place, bump to 0.4.0.

Edge cases:

- An install without package metadata.

Acceptance criteria:

- `docs/SECURITY.md` and `README.md` describe the two keys, the size caps,
  the rate limits for every route, the home folder bound for resumes, and the
  ngrok risk with Tailscale as the next step. CHANGELOG has 0.4.0.

### Ticket 18 - Phase 3 audit

Objective:

- Close the pass.

TDD work:

- List what tickets 14 to 17 left untested, add the tests, fix what they
  expose. Then the final audit: all suites, lint, a manual phone check.

Edge cases:

- An old phone build with the key baked in must stop working after the key
  rotation.

Acceptance criteria:

- All checks green locally and in CI. Ready for Ken's review and a PR into
  `dev`.

## Phase 4: files from the phone

### Ticket 19 - Read-only files for the phone

Objective:

- The phone can browse, play, view and download the files in the shared
  folders, and nothing else on the Mac.

TDD work:

- Failing tests: roots come from `ANOTHER_BRIDGE_FILE_ROOTS` and never
  expose absolute paths; a bad root (missing, not a folder, outside home,
  home itself, a bad or repeated name) stops the boot; listings hide
  dotfiles, key files and links that leave the root, and sort folders
  first then newest first; `..`, absolute paths, NUL bytes and hidden
  parts are refused; raw files are served inline with Range support;
  `locate` maps chat paths to a root and refuses anything not shared;
  every route needs the phone key.
- Then `services/file_roots.py` and a thin `routers/files.py`.

Edge cases:

- A symlink inside the root that points inside it works; one that points
  out is hidden and refused.
- A folder with more than 500 entries is cut off and marked `truncated`.

Acceptance criteria:

- Default in `.env.example` is `outputs=~/agent_outputs`. Checked by hand
  once from the phone: a reel plays, its cover shows, its caption copies.

## Phase 5: phone sync

### Ticket 20 - Re-attach to a running turn

Objective:

- A phone that left the chat, was frozen by iOS or relaunched sees the
  rest of a running turn stream in live, not a 30 s poll of the
  transcript under a "reconnecting" label.

TDD work:

- Failing tests: every resume path publishes its turn's text and tool
  events to a live run (`services/live_runs.py`) from the reader thread,
  with or without a client; `/resume/attach` replays the message being
  written now (not finished ones, which are in the transcript), follows
  live, sends keepalives and ends with `done`; nothing running ends with
  `done` at once; the live run opens with the in-flight marker and closes
  with it, under the same lock; queued text turns stream instead of
  blocking.
- Then the code.

Edge cases:

- A follower that attached before the turn ended still reads its last
  events.
- A queued turn claims the session right after the previous one ends:
  its new live run is not closed by the old one.
- A queued text turn used to be cut at 300 s (`run_blocking`); it now
  streams with no deadline, like a live send. Ticket 8 adds one deadline
  for every path.

Acceptance criteria:

- 421 tests pass. Checked by hand once from the phone: leave a chat mid
  turn, come back, and the reply keeps streaming.

### Ticket 21 - One copy of a session at a time

Objective:

- A phone message never starts a second copy of a session that is working
  in a terminal tab. On 2026-10-27 a phone turn ("still waiting?") resumed
  Marketing 1 while its terminal tab waited on 3 reviewer agents. The
  phone copy couldn't see them, decided the designer had stopped,
  messaged it (starting a second copy of it), and both copies wrote the
  GR00T script and sent the reviewers a second round.

TDD work:

- Failing tests: `services/terminal_activity.py` reads Claude Code's
  `sessions/<pid>.json` files and calls a session active when another
  live process holds it and is busy, or its own lines (by `entrypoint`) or
  its agents' transcripts changed within 120 s; the bridge's own children
  and lines never count; garbage and dead pids are skipped; errors fail
  open. Routes: a held stream queues with a `queued` event; a quiet
  terminal tab doesn't hold; `force` and `/resume/queue/release` run it;
  `/resume/queue` waits too; `/resume` gives 409; voice turns queue; the
  cap holds the next turn and names the sessions holding it; cards show
  `terminal`.
- `scripts/terminal_catch_up.py`, a `UserPromptSubmit` hook for terminal
  tabs: hands over the phone turns the tab hasn't seen, once, capped at
  30 turns, with images saved like /sync-mobile.
- Then the code.

Edge cases:

- "busy" alone misses the GR00T case: the tab's turn had ended and it
  waited, idle, on background agents. Agent transcripts carry the
  entrypoint of the process running them, so the tab's agents are told
  apart from a phone copy's.
- Claude Code's process files are internal and may change: unreadable
  means no hold (the old behavior), with one warning in the log.
- The cap partly covers ticket 9 for phone turns only.

Acceptance criteria:

- 450 tests pass. The bridge restart, the hook in `~/.claude/settings.json`
  and app 1.6.0 go live only when Ken says to install.

### Ticket 22 - Card time from the last real message

Objective:

- A session card says when Ken last used the session. On 2026-11-04 the
  Nov/1 LunchyBox card said "41 min ago" two days after its last message:
  the card used the transcript's mtime, and open Claude Code tabs touch
  their transcripts with no new turn (every idle tab at 19:23:21 the same
  second, right after the Mac woke, no bytes added). The list order used
  the same time, so idle open tabs jumped to the top.

TDD work:

- Failing tests (`tests/test_session_cards.py`): `last_activity` is the
  last user or assistant event's timestamp, not the file time; idle
  compaction, its summary turn, meta turns, subagents and metadata lines
  don't count; a tool result counts; no timestamps falls back to mtime;
  the list sorts by it.
- Then the code in `services/session_index.py`.

Edge cases:

- The field keeps its name and type, so the app needs no change.

Acceptance criteria:

- The tests pass; the card cache still keys on (mtime, size).

### Ticket 23 - Card title from the newest name

Objective:

- A name set with `claude --name` or `/rename` shows on the phone. The
  index took the first `ai-title` and ignored `custom-title`.

TDD work:

- Failing tests: the latest `custom-title` wins over any `ai-title`; the
  latest `ai-title` wins over earlier ones; a blank custom title is
  ignored.
- Then the code.

Acceptance criteria:

- The tests pass.

### Ticket 24 - Phone turns run long work in the foreground

Objective:

- Long work asked for from the phone finishes. On 2026-11-04 a reel
  builder agent launched from a phone turn stopped ("[Request interrupted
  by user]") exactly 10 minutes after the turn's reply. A Haiku test on
  2026-11-05 showed the same: a headless `claude -p` waits about 10
  minutes for its background tasks after the reply, then stops them and
  exits. The bridge has no timer on the stream path, so the cut is Claude
  Code's. The stream's `result` event also comes only at that exit.

TDD work:

- Failing tests: every phone turn (stream, blocking, voice, new session)
  carries `--append-system-prompt` with `PHONE_TURN_NOTE`, which says to
  run long work in the foreground, without `run_in_background`.
- Then the code (`_phone_flags` in `routers/sessions.py`).

Edge cases:

- A foreground build holds one of the 3 turn slots for its whole run.
- The phone streams a turn with no deadline, and re-attach covers a
  dropped stream, so a long foreground turn is safe to watch.

Acceptance criteria:

- The tests pass. The next reel started from the phone runs to the end.

### Ticket 25 - New session from the phone, Open on Mac

Objective:

- Ken starts a new session from the phone and continues it on the Mac,
  and opens any session in a new Mac terminal tab from the phone.

TDD work:

- `services/mac_terminal.py`: the `cd <folder> && claude
  --dangerously-skip-permissions --resume <id>` line (shell-quoted, id
  shaped, no control characters), the iTerm2 script (new tab in the front
  window, a new window when none is open, the line typed into the shell),
  the Terminal script, the app choice (`auto` prefers iTerm2 and falls
  back to Terminal), osascript errors, and the boot check of the settings.
- `POST /sessions/new/stream`: the bridge picks the id
  (`--session-id`), passes `--name`, runs in
  `ANOTHER_BRIDGE_NEW_SESSION_CWD`, and sends a `session` event once the
  transcript exists (so the phone can open the chat and attach to the
  first turn), then the usual events. The turn cap refuses it with
  `error` reason `limit`; `force` passes it.
- `POST /sessions/{id}/open-on-mac`: `opened`, `waiting` (a phone turn is
  writing the session or messages are queued; it opens when they end,
  once, within `ANOTHER_BRIDGE_OPEN_WAIT_SECONDS`), or `already_open` (a
  terminal copy holds it). 404, 422 (no folder), 502 (osascript failed).
  `/resume/status` shows `opening_on_mac`.

Edge cases:

- The first tap on a Mac may need macOS to allow the bridge to control
  iTerm2 or Terminal (Settings, Privacy and Security, Automation); until
  then the tap answers 502 with osascript's message.
- The command is typed into the tab's shell, so Ken's PATH applies and
  the tab stays open after claude exits.
- Claude Code never saves folder trust for the home folder, so a tab
  opened there starts at "trust this folder" (default "No, exit"), like
  any `claude` started there by hand. Until it is answered the process
  has no `sessions/<pid>.json` record, so the first live check opened a
  second tab on a second tap. `already_open` now also counts any
  `--resume <id>` process that isn't the bridge's own child.

Acceptance criteria:

- 516 tests pass. Manual check on 2026-11-05 through the tunnel: a new
  session started (`session`, `text`, `done` in 6 s) with its name on the
  card; Open on Mac opened an iTerm2 tab with no macOS permission prompt;
  a session open in a tab answered `already_open`.

### Ticket 26 - Talk to a running turn, queue actions

Objective:

- A message Ken sends while Claude works can go into the running turn
  (to update or take back what he asked), or wait in the queue as
  before. Queued messages can be sent now, deleted (with Undo) or edited,
  and a running turn can be stopped.

TDD work:

- `services/turn_input.py`: the stream-json first message, a message
  sent mid-turn (recorded before the write so its echo can't come
  first), `read` on claude's echo, the interrupt line, nothing written
  after close or a broken pipe.
- Routes (`tests/test_turn_input_and_queue.py`, with a fake turn that
  runs until the test ends it): inject while running, 409 after the
  `result` and when nothing runs; Stop interrupts and pauses the queue,
  Run queue resumes it; ids, Delete and Undo, Edit keeps files; Send now
  into the running turn and past a terminal hold; Send all now in order.
- `tests/conftest.py` refuses a real `streaming_subprocess_open` in any
  test, since a phone turn runs with `--dangerously-skip-permissions`.

Edge cases:

- Before building, a Haiku test checked the CLI: a message written
  while a 15 s command ran was read right after it and the one reply
  included it; an interrupt ended the turn at once.
- A message sent as the turn ends is either read as the next turn by
  the same process (written before stdin closed) or refused with 409 and
  queued by the phone. Nothing is lost.
- Voice (schema) and blocking turns keep the closed stdin: they can't
  take messages or be stopped, and Send now puts the message next.

Acceptance criteria:

- 535 tests pass.

### Ticket 27 - End a finished turn kept alive in the background

Objective:

- A session never waits on a phone turn that already replied. On
  2026-11-05 a Kodama turn replied at 18:06 and left `shopify theme dev`
  running in the background. Its `claude -p` stayed alive, so the bridge
  counted it as running and the 2 queued messages waited 40 minutes,
  with no Stop that worked.

TDD work:

- `TurnInput.saw_line` and `lingering`: lingering only after a `result`
  and no output since; a later line (a background notice starting a new
  turn) resets it.
- A watcher per streamed turn ends the process group after
  `ANOTHER_BRIDGE_LINGER_SECONDS` of that; a quiet turn still writing
  its reply is never ended; the queue moves on.
- Stop ends a process whose input is already closed.
- `PHONE_TURN_NOTE` says not to leave servers or watchers running.

Acceptance criteria:

- 540 tests pass.

### Ticket 28 - Delete a session from the phone

Objective:

- Ken deletes a conversation from the phone, with checks fit for a
  critical action.

TDD work (`tests/test_delete_session.py`):

- The body must repeat the id (422 otherwise, also with no body).
- Busy (a phone turn running or messages queued) and open in a Mac tab
  are refused with 409; unknown is 404.
- Nothing is erased: the transcript, its folder and the phone
  attachments move to a trash folder with an `origin.txt`.

Acceptance criteria:

- 546 tests pass. App 1.10.0 asks twice (a warning, then typing DELETE).

### Ticket 29 - What runs on the Mac, and stop a command

Objective:

- Ken sees from the phone every Bash command Claude runs on the Mac, in
  Mac tabs and phone turns, and can stop one (the stuck `shopify theme
  dev` of 2026-11-05 is the case).

TDD work (`tests/test_processes.py`, `services/process_view.py`):

- `ps` parsing, running times, the command out of the Bash tool's
  wrapper (with and without `< /dev/null`, heredoc newlines shown as
  `\012`), secrets redacted (keys, Bearer tokens, key headers, KEY=...).
- The snapshot: origin phone (a child of the bridge), Mac tab (`cli`) or
  headless; only Bash commands, not helpers; dead records skipped.
- Stop only a Bash command of a live Claude process, never a Claude
  process, the bridge or anything else; the pid twice.

Acceptance criteria:

- 567 tests pass.

### Ticket 30 - Stop a session wherever it runs

Objective:

- One Stop per session from the phone, whether Claude works in a phone
  turn or in a Mac terminal tab, optionally with every command it runs.

TDD work (`tests/test_stop_session.py`):

- A working Mac tab gets Esc (`mac_terminal.press_escape` on the tty of
  its Claude process); an idle tab gets nothing; a tab outside iTerm2 is
  `unsupported`; a phone turn goes through the /resume/stop path;
  `commands` ends each Bash command's group; the id twice; 404; 502 when
  osascript fails.
- `tty_of`, and the AppleScript only takes a `/dev/ttysN` and only sends
  Esc.

Edge cases:

- Checked 2026-11-05 on a throwaway iTerm2 tab running `cat -v`: the Esc
  found by tty reached that tab only.

Acceptance criteria:

- 584 tests pass.

## Phase 6: private network, always on

### Ticket 31 - Private network with Tailscale, ngrok off

Status: Todo (created 2026-11-07).

Objective:

- Only Ken's own devices can reach the bridge. Today the fixed ngrok URL
  is public and the phone key is the only gate. The key also sits in
  plain text inside the app's JS bundle (checked 2026-11-07), so a copy
  of the app works as a shell on the Mac.
- Tailscale (a private WireGuard network for Ken's devices) joins the Mac
  and the iPhone. `tailscale serve` publishes the bridge at
  `https://<mac>.<tailnet>.ts.net` on the tailnet only, with a real
  certificate. uvicorn stays on 127.0.0.1:8000. ngrok stops.

Setup (Ken, needs his logins):

- Install Tailscale on the Mac and on the iPhone, signed in to the same
  account.
- In the Tailscale admin console, turn on MagicDNS and HTTPS
  certificates.
- On the iPhone, turn on VPN On Demand so it reconnects by itself. iOS
  runs one VPN at a time.

Bridge work:

- `tailscale serve --bg 8000` (the config survives restarts). Never
  `tailscale funnel`, which makes the bridge public again.
- Guard: requests now arrive from 127.0.0.1 through serve. Confirm what
  serve puts in `X-Forwarded-For` so the lockout counts per device, not
  one shared localhost bucket; adjust `client_address` if needed.
- Second gate: require the `Tailscale-User-Login` header serve adds to
  equal `ANOTHER_BRIDGE_TAILSCALE_LOGIN`, 401 when missing or different,
  off when the variable is unset. First verify serve sets this header
  itself and drops a client-sent copy.
- Stop ngrok and take it out of the start steps; keep it in the README
  only as a documented fallback.
- Docs: SECURITY.md trust boundary (tailnet instead of a public URL),
  README start steps, CURL_EXAMPLES.md.

TDD work (`tests/test_guard.py`):

- `client_address` from serve's forwarded header.
- Login gate: match, mismatch, missing header, variable unset.

App side (another_bridge_mobile ticket 33):

- `EXPO_PUBLIC_BRIDGE_URL` becomes the ts.net address, rebuilt with a new
  version.

Edge cases:

- Tailscale off on the phone: the app shows offline, nothing reaches the
  Mac.
- Mac restart: the Tailscale app starts at login and serve keeps its
  config, but the bridge still needs starting by hand (the 2026-11-07
  restart left it down).
- AnotherAgent surface: since 2026-10-24 the log holds a single
  AnotherAgent request (a 401 from an unknown address), so nothing else
  needs ngrok. If the platform needs the bridge again, that is its own
  ticket (a cloud server cannot join the tailnet as simply).
- Confirm the current Tailscale free plan limits at setup.

Acceptance criteria:

- On cellular with Tailscale on: list, chat, Files and On the Mac work.
- With Tailscale off, and from any other device: no response.
- The old ngrok URL answers as offline.
- All tests pass.

### Ticket 32 - Start at login with launchd

Status: Done (2026-11-07).

Objective:

- After a Mac restart the phone works as soon as Ken logs in, with
  nothing started by hand. On 2026-11-07 a restart left the bridge and
  ngrok down until they were started again from a terminal.
- `scripts/bridge_service.py` installs the server and ngrok as launchd
  services (`com.kenruizinoue.another-bridge.server` and `.ngrok` in
  `~/Library/LaunchAgents`): they start at login and launchd starts
  either again when it stops. `on`, `off`, `restart`, `status`, each for
  both or only `server` or `ngrok` (`off ngrok` for ticket 31).

TDD work (`tests/test_bridge_service.py`, a fake launchctl/lsof/ps/kill):

- The server plist runs uvicorn on 127.0.0.1 through Ken's login shell
  (`zsh -lic`), so phone turns get the terminal's PATH (`claude` is under
  nvm). The ngrok plist uses `ANOTHER_BRIDGE_NGROK_URL` from `.env`,
  https only.
- `on` stops a bridge or an ngrok for the same URL started by hand,
  leaves any other program on port 8000 alone, reloads an installed
  service, and reports a failed bootstrap.
- `on`, `off` and `restart` refuse while a phone turn runs on the bridge
  (stopping the server cuts its reply) unless `--force`.

Edge cases:

- Checked 2026-11-07 on the real Mac: switched from the hand-started
  bridge, `/health`, `/sessions` and `/processes` answer through ngrok,
  the server's PATH has nvm, a killed server was back in 2 s, and
  osascript to iTerm2 from the launchd Python ran with no permission
  prompt (Open on Mac).
- `~/.another_coder/restart_after_turn.sh` lets launchd start the new
  bridge when the service is loaded, instead of starting a second one.

Acceptance criteria:

- 612 tests pass. Verified live as above.

## Phase 7: sessions on the Mac

### Ticket 33 - Phone-made sessions in /resume

Status: Done (2026-11-11).

Objective:

- A session started from the phone shows in Claude Code's /resume picker
  like a session started in a terminal. Ken created "2026/Nov/11
  AnotherStorage" from the phone and could not find it in /resume.
- Cause, read in Claude Code 2.1.319: /resume skips a session when the
  first `"entrypoint":"` value in the first 64 KB of its transcript is
  `sdk-cli`, `sdk-ts` or `sdk-py`. Every phone turn runs `claude -p`
  (`sdk-cli`), so only sessions whose first turn came from the phone were
  hidden.
- Fix (`services/resume_listing.py`): when a phone turn ends and nothing
  else holds the session (no queued message, no Mac tab), the bridge
  changes that first value to `cli` in place, padded with spaces after
  the closing quote, so the line keeps its length and stays valid JSON.
  Only the first value changes; later phone turns keep `sdk-cli`, which
  the terminal catch-up hook and the busy check rely on.

TDD work (`tests/test_resume_listing.py`):

- A port of Claude's check sees `cli` after the mark; same file size,
  every line still parses, later values untouched; a terminal session and
  a second run change nothing; no value in the first 64 KB is left alone;
  lines appended around the mark survive.
- The end of a phone turn marks the session; it waits while a message is
  queued or a Mac tab holds it; a terminal session never scans for
  holders; a missing transcript or a failure never breaks a turn's end.

Edge cases:

- An older phone-made session is marked after its next phone turn.
  AnotherStorage was marked by hand on 2026-11-11 (822 lines, all parse).
- If a Claude Code update changes the check, the sessions are only hidden
  again; nothing breaks. Recheck the rule when /resume misses one.

Acceptance criteria:

- 623 tests pass. AnotherStorage passes the ported check.

## Optional, not scheduled

- AnotherTask link (idea, 2026-11-08): AnotherTask MCP server, AI time
  from hooks, private web chat. See `docs/ANOTHERTASK_LINK_IDEA.txt`.
- Answer Claude Code's "trust this folder" question in tabs that Open on
  Mac opens (2026-11-08): read the screen first, only that tab, only in
  iTerm2. Waiting on Ken's go.
- A menu bar icon (a SwiftBar plugin) with status, On, Off and Restart
  calling `scripts/bridge_service.py` (2026-11-08).
- A lock file for exact dependency versions.
- E2E tests, only after Ken approves.

## Verification commands

```bash
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
uvicorn main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```
