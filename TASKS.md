# TASKS — another_coder

## Now — pre-publish

- [ ] **Add shared-secret header auth on webhook + bridge routes.** Single `X-API-Key` header, checked via a FastAPI `Depends` dependency, applied to `/tools/*` and `/chat/stream`. Constant-time compare (`hmac.compare_digest`) against an env var (`ANOTHER_CODER_API_KEY`). Reject with 401 on missing/mismatch. Add the var to `.env.example`, document key generation in README (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), and note that the same key goes in each platform tool config's headers. `/health` stays unauthed for ngrok / uptime checks. Skip JWT, OAuth, per-tool keys, and rate limiting — out of scope for v1.

## Later (only if a real user asks)

- [ ] Per-tool keys (rotate independently).
- [ ] Rate limit per key.
- [ ] Audit log of authed calls.
