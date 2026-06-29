# syntax=docker/dockerfile:1.7
#
# Single-image Tether host. The host process spawns `pi` (the agent) as an
# in-process Node subprocess and runs fastembed/ONNX in-process, so one container
# carries Python (uv) + Node + the agent's installed deps + the built SPA. The
# repo layout (apps/host, apps/agent, apps/web) is preserved in the image because
# the host resolves the agent and the SPA by walking up from its own package
# directory (`pi_runtime._repo_root()` → parents[3]).

# ---- Stage: build the SPA --------------------------------------------------
FROM node:25-bookworm-slim AS web-build
RUN npm install -g pnpm@10.33.4
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm build

# ---- Stage: install the agent (pi) Node deps ------------------------------
FROM node:25-bookworm-slim AS agent-deps
RUN npm install -g pnpm@10.33.4
WORKDIR /app/apps/agent
COPY apps/agent/package.json apps/agent/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/agent/ ./

# ---- Stage: runtime --------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime

# The host launches `pi` via `node`; copy the Node runtime in (no npm/pnpm needed
# at runtime — pi is invoked through its installed bin shim). `libatomic1` is the
# one shared lib the Node binary needs beyond the slim base; `ca-certificates`
# lets pi reach the LLM provider over HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node:25-bookworm-slim /usr/local/bin/node /usr/local/bin/node

ENV UV_PROJECT_ENVIRONMENT=/app/apps/host/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/apps/host/.venv/bin:$PATH
WORKDIR /app/apps/host

# Resolve Python deps from the lockfile first so the layer caches across source
# edits. `--no-dev` skips the dev group (pyright/ruff/snektest), so the editable
# `snektest` path source is never needed in the image.
COPY apps/host/pyproject.toml apps/host/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Lay out the three apps in the layout the editable install + `_repo_root()`
# expect, then install the host package itself (editable, so `import tether`
# resolves to /app/apps/host/tether and the agent/SPA paths resolve relative to
# /app).
COPY apps/host/ /app/apps/host/
COPY --from=agent-deps /app/apps/agent/ /app/apps/agent/
COPY --from=web-build /app/apps/web/dist/ /app/apps/web/dist/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Production defaults; secrets and HTTPS-dependent toggles come from the
# environment / compose (.env). Paths point at the mounted volumes.
ENV TETHER_HOST=0.0.0.0 \
    TETHER_PORT=8000 \
    TETHER_WEB_DIST=/app/apps/web/dist \
    FASTEMBED_CACHE_PATH=/cache/fastembed

EXPOSE 8000
CMD ["python", "-m", "tether"]
