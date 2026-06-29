# Tether tasks. `just` from root; uv targets apps/host via UV_PROJECT (.envrc).

default:
    @just --list

# Python host (Starlette, auto-reload)
host:
    TETHER_RELOAD=true TETHER_APP_PASSWORD=dev TETHER_SESSION_SECRET=dev-session-secret uv run python -m tether

# SolidJS web (Vite dev server)
web:
    pnpm -C apps/web dev

# one-time YouTube OAuth bootstrap (caches a token, prints recent liked titles)
# set TETHER_YOUTUBE_OAUTH_NO_BROWSER=1 to print the URL instead of opening a browser
# --group youtube ensures the optional Google client libraries are installed first
youtube-auth:
    uv run --group youtube python -m tether.youtube_auth

# sync/install all deps
install:
    uv sync
    pnpm -C apps/web install
    pnpm -C apps/agent install

# start host, exercise a few requests, then print captured stdout logs
validate-host-logs:
    ./scripts/validate-host-logs.sh

# boot host + web on ephemeral ports, drive headless Chromium, fail on page errors
validate-web-smoke:
    ./scripts/validate-web-smoke.sh

# code generation
codegen:
    uv run python -m tether.openapi_export openapi.json
    pnpm -C apps/web codegen
    pnpm -C apps/web format:generated
    uv run python -m tether.tool_schemas apps/agent/generated/tool-schemas.json
    pnpm -C apps/agent codegen
    pnpm -C apps/agent format:generated

# generated-code drift check
codegen-check:
    just codegen
    git diff --exit-code -- openapi.json apps/web/src/generated apps/agent/generated/tool-schemas.json apps/agent/src/generated

# host tests
host-test:
    cd apps/host && uv run python -m snektest tests/

# host type check
host-typecheck:
    uv run pyright

# host lint
host-lint:
    uv run ruff check .

# host format check
host-format-check:
    uv run ruff format --check .

# agent tests
agent-test:
    pnpm -C apps/agent test

# agent type check
agent-typecheck:
    pnpm -C apps/agent typecheck

# agent lint
agent-lint:
    pnpm -C apps/agent lint

# agent format check
agent-format-check:
    pnpm -C apps/agent format:check

# all tests
test: host-test agent-test

# all type checks
typecheck: host-typecheck agent-typecheck

# all lint checks
lint: host-lint agent-lint

# all format checks
format-check: host-format-check agent-format-check

# validate a compose env file before starting the app
validate-env env_file=".env":
    #!/usr/bin/env bash
    set -euo pipefail
    test -f "{{env_file}}" || (echo "missing {{env_file}}; copy .env.example to .env" >&2; exit 1)
    TETHER_ENV_FILE="{{env_file}}" uv run python - <<'PY'
    import os
    import sys
    from pathlib import Path

    from dotenv import dotenv_values

    from tether.model_selection import AgentModelCatalog
    from tether.server import HostSettings

    env_file = Path(os.environ["TETHER_ENV_FILE"])
    values = dotenv_values(env_file)
    errors = []

    for key in ["TETHER_APP_PASSWORD", "TETHER_SESSION_SECRET", "TETHER_DEFAULT_MODEL", "TETHER_MODEL_ALLOWLIST"]:
        value = values.get(key)
        if value is None or value == "" or value == "change-me":
            errors.append(f"{key} must be set in {env_file}")

    for key, value in values.items():
        if value is not None:
            os.environ[key] = value

    try:
        settings = HostSettings()
        AgentModelCatalog(default_model=settings.default_model, models=settings.model_allowlist)
        if any(model.provider == "anthropic" for model in settings.model_allowlist):
            errors.append("TETHER_MODEL_ALLOWLIST must use pi subscription providers, not anthropic")
    except Exception as exc:
        errors.append(str(exc))

    pi_agent_dir = values.get("TETHER_PI_AGENT_DIR") or "${HOME}/.local/share/tether/pi-agent"
    pi_agent_dir = os.path.expandvars(os.path.expanduser(pi_agent_dir))
    if not Path(pi_agent_dir).is_absolute():
        errors.append("TETHER_PI_AGENT_DIR must resolve to an absolute path")

    if errors:
        for error in errors:
            print(f"env error: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"{env_file} ok")
    PY
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" config --quiet

# start the whole app via docker compose; creates the pi credential dir if needed
app-start env_file=".env":
    #!/usr/bin/env bash
    set -euo pipefail
    just validate-env "{{env_file}}"
    dir=$(TETHER_ENV_FILE="{{env_file}}" uv run python - <<'PY'
    import os
    from pathlib import Path

    from dotenv import dotenv_values

    values = dotenv_values(os.environ["TETHER_ENV_FILE"])
    raw = values.get("TETHER_PI_AGENT_DIR") or "${HOME}/.local/share/tether/pi-agent"
    print(Path(os.path.expandvars(os.path.expanduser(raw))))
    PY
    )
    mkdir -p "$dir"
    chmod 700 "$dir"
    if [ ! -f "$dir/auth.json" ]; then
      echo "warning: $dir/auth.json not found; pi provider auth is not bootstrapped" >&2
    fi
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" up -d --build

# build + run the production image locally via docker compose (see docs/deploy.md)
deploy-local: app-start

# stop the local compose stack (keeps the data + model-cache volumes)
deploy-local-down:
    docker compose down
