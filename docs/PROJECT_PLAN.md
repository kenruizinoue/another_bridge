# another_bridge: security and quality pass (v0.4.0)

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

## Optional, not scheduled

- Tailscale: serve the bridge only on the private network, with an access
  rule for the iPhone.
- An ngrok traffic policy as a second gate.
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
