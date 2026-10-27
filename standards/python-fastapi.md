# Ken Python FastAPI Rules

Use with `base-sdlc.md`, usually after the Python core is tested.

## Purpose

Expose existing business logic through HTTP. FastAPI is an adapter, not a second implementation.

## Structure

Small projects:

```text
src/package_name/
  api.py
  domain.py
  processor.py
tests/test_api.py
```

Split later only when useful:

```text
src/package_name/api/
  app.py
  models.py
  routes.py
  dependencies.py
```

## Defaults

- `fastapi` for app/routes.
- `uvicorn` for local server.
- `TestClient` or HTTP client for API tests.
- Gate API deps behind an `api` extra so core/CLI stays lightweight (`pip install -e ".[api,dev]"`).

## API Rules

- Define request/response models.
- Validate enum-like modes explicitly.
- Route handlers should only validate, choose config, call core, and translate result.
- Return structured errors when clients need field/line-level messages.
- Do not calculate domain results in route handlers.

## Ticket Order

1. API dependency and app smoke test.
2. Extend Python CI to install API deps and run API tests.
3. Request/response models.
4. Main endpoint delegating to core.
5. Structured error output.
6. Config/mode selection.
7. Run docs.

## Error Pattern

- Use `4xx` for invalid request shape/config.
- Use structured result envelopes for partial domain failures when processing should continue.

## Verify

```bash
pytest
uvicorn package_name.api:app --reload
```
