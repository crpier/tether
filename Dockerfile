# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/app/apps/host/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/apps/host/.venv/bin:$PATH
WORKDIR /app/apps/host

# Resolve locked third-party dependencies before copying frequently changed source.
COPY apps/host/pyproject.toml apps/host/uv.lock ./
COPY packages/snekok/pyproject.toml packages/snekok/README.md /app/packages/snekok/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-install-package snekok

COPY apps/host/README.md ./README.md
COPY apps/host/tether/ ./tether/
COPY packages/snekok/src/ /app/packages/snekok/src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV TETHER_HOST=0.0.0.0 \
    TETHER_PORT=8000

EXPOSE 8000
CMD ["python", "-m", "tether"]
