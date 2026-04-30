# TASKS — another_coder

## Now — pre-publish

- [x] **Add shared-secret header auth on webhook + bridge routes.** Single `X-Coder-Key` header, checked via a FastAPI `Depends` dependency, applied router-level to `/chat/stream`, `/tools/*`, and `/jobs/*`. Constant-time compare (`hmac.compare_digest`) against env var `ANOTHER_CODER_API_KEY`. Reject with 401 on missing/mismatch, 503 when the env var is unset (refuses to silently allow-all). `/health` and `/verifyApiKey` (legacy) stay unauthed. Header name is intentionally distinct from the AnotherAgent platform's own `X-API-Key` (which scopes the tenant) so a single hop platform → bridge can carry both without collision. Skip JWT, OAuth, per-tool keys, and rate limiting — out of scope for v1.

## Later (only if a real user asks)

- [ ] Per-tool keys (rotate independently).
- [ ] Rate limit per key.
- [ ] Audit log of authed calls.
