# Tether tasks. Run `just` from the repository root.
export UV_PROJECT := justfile_directory() + "/apps/host"
export VIRTUAL_ENV := justfile_directory() + "/apps/host/.venv"
export TETHER_IMAGE := env_var_or_default("TETHER_IMAGE", "ghcr.io/crpier/tether")

set dotenv-load := true

default:
    @just --list

# Run only the headless Python capability host with local fallback credentials.
host:
    #!/usr/bin/env bash
    set -euo pipefail
    export TETHER_API_TOKEN="${TETHER_API_TOKEN:-dev-capture-token}"
    export TETHER_OPEN_WEBUI_TOKEN="${TETHER_OPEN_WEBUI_TOKEN:-dev-open-webui-token}"
    export TETHER_RELOAD=true
    uv run python -m tether

# Run the production-shaped host and Open WebUI stack in the foreground.
dev env_file=".env":
    #!/usr/bin/env bash
    set -euo pipefail
    just validate-env "{{env_file}}"
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" up --build

# Create local configuration with independent generated credentials.
bootstrap:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .env ]; then
      echo ".env already exists; leaving it untouched" >&2
      exit 0
    fi
    cp .env.example .env
    uv run python - <<'PY'
    import secrets
    from pathlib import Path

    path = Path(".env")
    text = path.read_text()
    for name in (
        "TETHER_API_TOKEN",
        "TETHER_OPEN_WEBUI_TOKEN",
        "WEBUI_SECRET_KEY",
    ):
        text = text.replace(
            f"{name}=change-me",
            f"{name}={secrets.token_urlsafe(48)}",
        )
    path.write_text(text)
    print("wrote .env with independent secrets; fill the model provider settings")
    PY

# Install Python packages and the standalone Playwright smoke dependencies.
install:
    uv sync
    cd packages/snekok && env -u UV_PROJECT -u VIRTUAL_ENV uv sync
    npm --prefix tests/open-webui ci

# Report legacy trigger-linked Todos without changing them.
todo-trigger-cleanup-report:
    uv run python -m tether.cleanup_linked_todos

# Clear legacy trigger links only after reviewing the report.
todo-trigger-cleanup-confirm:
    uv run python -m tether.cleanup_linked_todos --confirm

validate-host-logs:
    ./scripts/validate-host-logs.sh

# Real pinned Open WebUI + real host + fake provider + Chromium.
validate-open-webui-smoke:
    ./scripts/validate-open-webui-smoke.sh

host-test:
    cd apps/host && uv run python -m snektest tests/

host-typecheck:
    cd apps/host && uv run pyright

host-lint:
    cd apps/host && uv run ruff check .

host-format-check:
    cd apps/host && uv run ruff format --check .

snekok-test:
    cd packages/snekok && env -u UV_PROJECT -u VIRTUAL_ENV uv run python -m snektest tests/

snekok-typecheck:
    cd packages/snekok && env -u UV_PROJECT -u VIRTUAL_ENV uv run pyright

snekok-lint:
    cd packages/snekok && env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff check .

snekok-format-check:
    cd packages/snekok && env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff format --check .

open-webui-typecheck:
    npm --prefix tests/open-webui run typecheck

open-webui-lint:
    npm --prefix tests/open-webui run lint

open-webui-format-check:
    npm --prefix tests/open-webui run format:check

test: host-test snekok-test

typecheck: host-typecheck snekok-typecheck open-webui-typecheck

lint: host-lint snekok-lint open-webui-lint

format-check: host-format-check snekok-format-check open-webui-format-check

# Validate required independent credentials and resolved Compose configuration.
validate-env env_file=".env":
    #!/usr/bin/env bash
    set -euo pipefail
    test -f "{{env_file}}" || {
      echo "missing {{env_file}}; copy .env.example to .env" >&2
      exit 1
    }
    set -a
    source "{{env_file}}"
    set +a
    for name in TETHER_API_TOKEN TETHER_OPEN_WEBUI_TOKEN WEBUI_SECRET_KEY WEBUI_URL OPENAI_API_BASE_URLS OPENAI_API_KEYS; do
      value="${!name:-}"
      if [ -z "$value" ] || [ "$value" = change-me ]; then
        echo "$name must be set to a non-template value in {{env_file}}" >&2
        exit 1
      fi
    done
    if [ "$TETHER_API_TOKEN" = "$TETHER_OPEN_WEBUI_TOKEN" ]; then
      echo "TETHER_API_TOKEN and TETHER_OPEN_WEBUI_TOKEN must differ" >&2
      exit 1
    fi
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" config --quiet
    echo "{{env_file}} ok"

# Start the production-shaped local stack in the background.
app-start env_file=".env":
    #!/usr/bin/env bash
    set -euo pipefail
    just validate-env "{{env_file}}"
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" up -d --build --wait

deploy-local: app-start

deploy-local-down env_file=".env":
    TETHER_ENV_FILE="{{env_file}}" docker compose --env-file "{{env_file}}" down

# Build and push the host image; Open WebUI remains the official pinned image.
deploy-build:
    #!/usr/bin/env bash
    set -euo pipefail
    sha=$(git rev-parse --short HEAD)
    docker build -t "${TETHER_IMAGE}:${sha}" -t "${TETHER_IMAGE}:latest" .
    docker push "${TETHER_IMAGE}:${sha}"
    docker push "${TETHER_IMAGE}:latest"
    echo "pushed ${TETHER_IMAGE}:${sha} and :latest"

# Deploy only validated, merged origin/main. Pull the VM checkout first when
# Compose or deploy assets changed.
deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    test "$(git branch --show-current)" = main || {
      echo "deploy requires the main branch" >&2
      exit 1
    }
    test -z "$(git status --porcelain)" || {
      echo "deploy requires a clean worktree" >&2
      exit 1
    }
    git fetch origin main
    test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" || {
      echo "local main must exactly match origin/main" >&2
      exit 1
    }
    just deploy-build
    host="${TETHER_DEPLOY_HOST:?set TETHER_DEPLOY_HOST=user@vm}"
    dir="${TETHER_DEPLOY_DIR:-/srv/tether}"
    ssh "$host" "cd '$dir' && git pull --ff-only origin main && docker compose pull && docker compose up -d --wait"
    echo "deployed $(git rev-parse --short HEAD) to $host:$dir"

# Ordinary post-migration host-image rollback. Full migration rollback must also
# restore the recorded old Git revision and Pi credential directory.
deploy-rollback sha:
    #!/usr/bin/env bash
    set -euo pipefail
    host="${TETHER_DEPLOY_HOST:?set TETHER_DEPLOY_HOST=user@vm}"
    dir="${TETHER_DEPLOY_DIR:-/srv/tether}"
    ssh "$host" "cd '$dir' && sed -i '/^TETHER_IMAGE_TAG=/d' .env && echo 'TETHER_IMAGE_TAG={{ sha }}' >> .env && docker compose pull host && docker compose up -d host"
    echo "rolled back the post-migration host image on $host:$dir to {{ sha }}"

# Follow structured logs for one service, or both services when omitted.
logs service="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=()
    if [ -n "{{service}}" ]; then args+=("{{service}}"); fi
    docker compose logs -f --no-log-prefix "${args[@]}"

# Health Connect-only Android gate. Requires Android SDK and JDK 17.
android-build:
    #!/usr/bin/env bash
    set -euo pipefail
    for jdk in /usr/lib/jvm/java-17-openjdk /usr/lib/jvm/java-21-openjdk; do
      if [ -x "$jdk/bin/java" ]; then export JAVA_HOME="$jdk"; break; fi
    done
    cd apps/capture-android
    ./gradlew :app:assembleDebug :app:testDebugUnitTest :app:lintDebug :core:test
