# Ken SDLC Base Rules

Use this as the central project guide. Platform files only add platform-specific choices.

## Principles

- Treat the project as code that may become part of a larger system.
- Separate core scope from optional demo or expansion scope.
- Deliver in small ticket-based increments.
- Core workflow works before optional platform extensions.
- Keep business logic independent from CLI, API, and UI adapters.
- Do not duplicate domain logic across layers.

## Planning Output

Create `docs/PROJECT_PLAN.md` before implementation:

- goal and expected behavior
- phases
- ticket list
- acceptance criteria per ticket
- verification commands

Ticket shape:

```md
### Ticket N - Name

Objective:

- ...

TDD work:

- ...

Edge cases:

- ...

Acceptance criteria:

- ...
```

## TDD Rules

- Ticket 1 wires the test runner with a smoke test only.
- For behavior tickets: failing test, implementation, refactor.
- Test core/domain rules first.
- Unit and integration tests should be added in the same ticket as the feature they verify.
- Add regression tests for discovered bugs.
- Plan an E2E ticket near the end, but do not implement E2E automatically. Wait for explicit user approval because UI/workflows may still be changing.

## Architecture Rules

- Core modules own business rules.
- Adapters stay thin:
  - CLI parses args and delegates.
  - API validates/translates and delegates.
  - UI captures intent and calls a gateway/adapter.
- Make hinted variation configurable: random source, policies, currency/locale, network layer.
- Add extension points where requirements suggest future change; avoid speculative abstractions.

## Error Handling

- Domain errors should be clear and testable.
- Adapters translate errors into their surface.
- Invalid configuration should be rejected, not silently hidden.

## Writing Style

- Never use em dashes in anything produced: code, comments, docs, commit messages, output.

## Docs

- `README.md`: setup, run, test, usage.
- `docs/PROJECT_PLAN.md`: ticket status.
- `docs/ARCHITECTURE.md`: layer boundaries when multiple platforms exist.
- Assessment-specific AI/process docs stay separate.

## Config and Secrets

- Read config from env vars with safe dev defaults; never hardcode secrets.
- Commit `.env.example`; gitignore real env files.

## CI

- Use GitHub Actions; run tests on every push and PR to the default branch.
- Each platform gets its own CI job/workflow when the platform is configured; include a CI ticket right after that platform's setup ticket.
- CI runs lint, unit/integration tests, and build/typecheck. E2E runs in CI only after E2E is approved.
- Workflow hygiene: `permissions: contents: read`, concurrency group with cancel-in-progress, `timeout-minutes`, dependency caching.
- A phase is not done until its CI is green on the default branch.

## Git and Verification

- Keep one long-lived branch and make it GitHub's default branch: name it `main` in new repos; existing repos keep the name they already use (renaming breaks CI and deploy triggers). No `dev` or staging integration branch; feature branches merge straight into it and are deleted after merging.
- Create a feature branch before any implementation commit; never commit directly to the default branch. Name it descriptively (e.g. `feature/<slug>` or a ticket-based name).
- Never commit unless the user explicitly asks for a commit. Approval to plan, implement, or solve a ticket is not approval to commit. Solving or implementing a ticket ends by running checks and updating the plan status, then pausing for review.
- When the user asks for a commit, commit only completed, verified increments.
- Keep generated files out of git.
- Before final: run unit, integration, build/typecheck, approved E2E if present, and one manual primary workflow check.
- Confirm CI is green after the final push.
- Open a PR against the default branch as the default delivery method; only skip or retarget it if the requirement explicitly says otherwise.
