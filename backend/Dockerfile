FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM python:3.14-slim

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY tether ./tether
RUN uv sync --locked --no-dev

RUN useradd --create-home tether
USER tether

EXPOSE 8000
CMD ["python", "tether/main.py", "--host", "0.0.0.0"]
