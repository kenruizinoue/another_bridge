# syntax=docker/dockerfile:1.6
#
# another_bridge bridge — production-ish Docker image.
#
# Why this exists: Docker isn't the recommended path for a laptop
# demo (just `python -m venv` + `pip install`), but for users
# running the bridge on a VPS / home server it collapses the
# install matrix (Python + Node + claude CLI + Python deps) into
# one `docker build`. The Dockerfile installs:
#
#   - Python 3.11 (matches the CI workflow)
#   - Node 20 (Claude Code CLI is published as an npm package)
#   - The claude CLI itself, globally
#   - The Python deps from requirements.txt
#
# Two host mounts are required at runtime — see `docker-compose.yml`
# in the repo root for the canonical example. tl;dr:
#
#   - Mount your workspace into /workspace (so Claude Code can
#     read + write your repos). Set WORKSPACE_ROOT=/workspace
#     in the env passed to the container.
#   - Mount your host's ~/.claude into /root/.claude (so the
#     containerized claude CLI inherits your authenticated
#     session — no need to re-login per restart).
#
# Run with:
#
#   docker run --rm -p 8000:8000 \
#       -v "$HOME/your-repos:/workspace:rw" \
#       -v "$HOME/.claude:/root/.claude:rw" \
#       --env-file .env \
#       another_bridge

# ── Stage 1: install claude CLI from npm ──────────────────────────────
# Multi-stage so we don't drag npm + the rest of the Node toolchain
# into the final image. Only the resolved `claude` binary copies
# forward.
FROM node:20-bookworm-slim AS claude_cli

# Pin to a specific version of @anthropic-ai/claude-code in
# production. For now we install latest because the Claude Code CLI
# itself is the upstream of truth — version churn is fast enough
# that pinning blocks operator hotfixes more than it stabilizes.
RUN npm install -g @anthropic-ai/claude-code

# ── Stage 2: Python runtime + bridge code ────────────────────────────
FROM python:3.11-slim-bookworm AS bridge

# git + ssh client because the implementation runner shells out to
# `git push`. ca-certificates so HTTPS to GitHub + ngrok works
# without surprises. tini for proper signal forwarding (uvicorn's
# default reload kills don't always terminate the Claude subprocess
# tree without it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Install Node runtime so `claude` (which is a Node binary) can run.
# Slim variant to keep the image small; we don't need npm at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pull the globally-installed claude binary + its node_modules from
# stage 1. The launcher script lives in /usr/local/bin/claude and
# resolves to the package under /usr/local/lib/node_modules.
COPY --from=claude_cli /usr/local/bin/claude /usr/local/bin/claude
COPY --from=claude_cli /usr/local/lib/node_modules /usr/local/lib/node_modules

WORKDIR /app

# Copy project metadata + source. Unlike the requirements.txt
# pattern (where deps could be installed before source for layer
# caching), pyproject.toml + setuptools needs the source files to
# build the package. Acceptable trade-off for a project this size —
# deps + source rebuild together when either changes. If layer
# caching becomes a real bottleneck later, switch to uv / pdm /
# pip-tools to lock deps into a separate file.
COPY pyproject.toml ./
COPY auth.py config.py jobs.py main.py ./
COPY routers/ ./routers/
COPY services/ ./services/

# Production install — runtime deps only, no dev/test extras.
RUN pip install --no-cache-dir .

# Defaults that align with the host-mount expectation. Operators
# override via --env-file or -e at runtime; these are just so
# `docker run` without a config still boots into a sane state.
#
# CLAUDE_BIN_PATH points at the global install we copied from
# stage 1. WORKSPACE_ROOT defaults to /workspace, which the
# container expects to be a host bind-mount.
ENV CLAUDE_BIN_PATH=/usr/local/bin/claude \
    WORKSPACE_ROOT=/workspace \
    ANOTHER_CODER_SESSION_DB_PATH=/data/sessions.db \
    PYTHONUNBUFFERED=1

# Mount targets. `/workspace` is the user's repo tree; `/data` is
# where SQLite + any future on-disk state lives so the operator
# can mount a Docker volume for persistence.
VOLUME ["/workspace", "/data"]

EXPOSE 8000

# Use tini so SIGTERM / SIGINT propagate to uvicorn AND its child
# Claude Code subprocess tree. Without tini, `docker stop` orphans
# the Claude processes; the reaper would clean them up on next
# boot, but that's a worse experience than a clean stop.
ENTRYPOINT ["/usr/bin/tini", "--"]

# `--host 0.0.0.0` so the bridge is reachable across the container
# boundary; ngrok / reverse-proxy forwarding runs on the host.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
