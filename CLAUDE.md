# another_bridge

FastAPI bridge that lets a phone continue the Claude Code sessions on this Mac
(`/sessions*`, `/tts`) and serves the older AnotherAgent webhooks. Every resume
runs `claude --resume` with `--dangerously-skip-permissions`, and the server is
reached over a public ngrok URL, so security changes come first.

## How to work in this repo

The engineering rules this repo follows are copied into `standards/`. Read them
from the source files each time; this file never summarizes or copies their
content, so nothing gets lost.

- Always mandatory: `standards/base-sdlc.md`.
- Stack rules: read `standards/python-fastapi.md` before working on any server
  ticket.
- Plan and ticket status: `docs/PROJECT_PLAN.md`. Work is delivered as numbered
  tickets, each ending with the verification commands in `README.md` and an
  update to the plan's status table.
- The default branch is `dev`. Work happens on a feature branch and merges into
  `dev` through a PR.
- The phone client lives in `../another_bridge_mobile`. A change to an endpoint
  contract needs the matching client change and a note in both changelogs.
