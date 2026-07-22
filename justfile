# Tether tasks. `just` from root; uv targets apps/host explicitly so recipes
# work from git worktrees even when a parent shell has stale direnv variables.
export UV_PROJECT := justfile_directory() + "/apps/host"
export VIRTUAL_ENV := justfile_directory() + "/apps/host/.venv"

# Load the repo-root `.env` into every recipe's environment. Without this the
# host boots with an empty `TETHER_MODEL_ALLOWLIST` (and no default model), so
# the chat model selector renders no choices. Recipe-line assignments (e.g.
# `TETHER_APP_PASSWORD=dev` in `host`/`dev`) still override the dotenv values.
set dotenv-load := true

default:
    @just --list

# Python host (Starlette, auto-reload)
host:
    TETHER_RELOAD=true TETHER_APP_PASSWORD=dev TETHER_SESSION_SECRET=dev-session-secret uv run python -m tether

# SolidJS web (Vite dev server)
web:
    pnpm -C apps/web dev

# fast dev loop: host (auto-reload) + web (HMR) together; open :3000 (docs/development.md)
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    # Job control: `&` jobs each get their own process group instead of
    # sharing this script's. That lets the cleanup trap below signal a job's
    # *whole* subtree — uv's child chain, uvicorn's `--reload` worker and its
    # multiprocessing reload-supervisor machinery, pnpm's vite/esbuild
    # children — instead of just the immediate uv/pnpm pid. Without this,
    # `kill "$host_pid"` only reaches `uv run`; deeper descendants are never
    # signaled and can outlive ctrl-c by minutes (e.g. while the host drains
    # an in-flight sync task; see `_shutdown_background_tasks` in
    # apps/host/tether/server.py for the in-process half of that fix).
    set -m
    echo "web → http://127.0.0.1:3000  (open this)   host → http://127.0.0.1:8000"
    # Logs stream to the terminal (colorized, TTY) *and* to files under
    # .tether/logs so an agent can read back what happened on a reported bug.
    # See docs/development.md#logs-and-session-data. Truncate once at launch so
    # each `just dev` run starts clean (the host appends across auto-reloads).
    mkdir -p .tether/logs
    : > .tether/logs/host.log
    : > .tether/logs/web.log
    # Ingestion syncs run enabled here: the boot pass is fire-and-forget (#122),
    # so a real OAuth token no longer blocks the :8000 bind, and `just dev` stays
    # in step with new liked videos. Set TETHER_YOUTUBE_SYNC_ENABLED=false /
    # TETHER_TRANSCRIPT_SYNC_ENABLED=false to opt out for a pure web/HMR loop.
    TETHER_RELOAD=true TETHER_APP_PASSWORD=dev TETHER_SESSION_SECRET=dev-session-secret \
        TETHER_LOGGING_LEVEL=DEBUG \
        TETHER_LOG_FILE=.tether/logs/host.log \
        uv run python -m tether &
    host_pid=$!
    # Process substitution (not a pipe) keeps $! as pnpm's pid so the trap kills
    # the dev server directly; tee mirrors Vite's output to the log file.
    pnpm -C apps/web dev > >(tee .tether/logs/web.log) 2>&1 &
    web_pid=$!
    cleanup() {
        # `-$host_pid`/`-$web_pid` (negative pid) targets each job's whole
        # process group (see `set -m` above), not just its immediate pid.
        # SIGTERM first for a graceful shutdown attempt, then SIGKILL after a
        # short grace period for anything still alive — guarantees ctrl-c
        # returns control within a few seconds even if the host is mid a slow
        # shutdown (e.g. draining a background sync task).
        kill -TERM -"$host_pid" -"$web_pid" 2>/dev/null || true
        sleep 2
        kill -KILL -"$host_pid" -"$web_pid" 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    # return as soon as either process exits; the trap tears the other down
    wait -n

# one-time local setup: write .env with generated secrets + create the pi-agent dir
bootstrap:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .env ]; then
      echo ".env already exists; leaving it untouched" >&2
    else
      cp .env.example .env
      uv run python - <<'PY'
    import secrets
    from pathlib import Path

    path = Path(".env")
    text = path.read_text()
    text = text.replace("TETHER_APP_PASSWORD=change-me", f"TETHER_APP_PASSWORD={secrets.token_urlsafe(32)}")
    text = text.replace("TETHER_SESSION_SECRET=change-me", f"TETHER_SESSION_SECRET={secrets.token_urlsafe(32)}")
    path.write_text(text)
    print("wrote .env with generated TETHER_APP_PASSWORD / TETHER_SESSION_SECRET")
    PY
    fi
    dir="${TETHER_PI_AGENT_DIR:-$HOME/.local/share/tether/pi-agent}"
    mkdir -p "$dir"
    chmod 700 "$dir"
    echo "pi-agent dir: $dir"
    echo "next: just pi-auth   (log in to your model provider)"

# one-time interactive pi provider login; writes auth.json into the pi-agent dir
pi-auth:
    #!/usr/bin/env bash
    set -euo pipefail
    dir="${TETHER_PI_AGENT_DIR:-$HOME/.local/share/tether/pi-agent}"
    mkdir -p "$dir"
    chmod 700 "$dir"
    echo "Launching pi against $dir. Run e.g. /login openai-codex, then exit."
    PI_CODING_AGENT_DIR="$dir" apps/agent/node_modules/.bin/pi

# one-time YouTube OAuth bootstrap (caches a token, prints recent liked titles)
# set TETHER_YOUTUBE_OAUTH_NO_BROWSER=1 to print the URL instead of opening a browser
# --group youtube ensures the optional Google client libraries are installed first
youtube-auth:
    uv run --group youtube python -m tether.youtube_auth

# one-time Gmail OAuth bootstrap (caches a token, prints a few recent subjects)
# reuses the YouTube OAuth client secret by default; set TETHER_GMAIL_CLIENT_SECRET_PATH
# to override. Set TETHER_GMAIL_OAUTH_NO_BROWSER=1 to print the URL instead of opening one
# --group youtube ensures the shared Google client libraries are installed first
gmail-auth:
    uv run --group youtube python -m tether.gmail_auth

# one-shot import of an active-workbench backup (likes + transcripts) into Tether
# never calls YouTube; idempotent; pass --dry-run to preview counts without writing
# e.g. just youtube-import-backup ~/active-workbench/state.db
youtube-import-backup *args:
    uv run python -m tether.youtube_import_backup {{args}}

# force a full liked-history resync: clears the backfill cursor + completion marker
# so the next sync pass re-walks history from the tail (never calls YouTube itself)
youtube-reset-backfill:
    uv run python -m tether.youtube_reset_backfill

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
    import json
    import os
    import sys
    from pathlib import Path

    from dotenv import dotenv_values

    from tether.model_selection import AgentModelCatalog
    from tether.server import HostSettings

    env_file = Path(os.environ["TETHER_ENV_FILE"])
    values = dotenv_values(env_file)
    errors = []

    for key in ["TETHER_APP_PASSWORD", "TETHER_SESSION_SECRET", "TETHER_STT_API_KEY", "TETHER_DEFAULT_MODEL", "TETHER_MODEL_ALLOWLIST"]:
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

    auth_path = Path(pi_agent_dir) / "auth.json"
    if not auth_path.exists():
        print(
            f"warning: pi auth not bootstrapped: {auth_path} missing (run `just pi-auth`)",
            file=sys.stderr,
        )
    else:
        try:
            json.loads(auth_path.read_text("utf-8"))
        except Exception as exc:
            errors.append(f"pi auth at {auth_path} is not valid JSON: {exc}")

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

# follow host container logs readable; `just logs <run_id>` filters one chat turn
logs run_id="":
    #!/usr/bin/env bash
    set -euo pipefail
    pipe() {
      if command -v jq >/dev/null 2>&1; then
        jq -rR 'fromjson? as $o | if $o == null then . else
          "\($o.timestamp // "") \(($o.level // "info") | ascii_upcase) \($o.logger // "") \($o.event // .)"
          + (if ($o.run_id // null) then "  [run \($o.run_id)]" else "" end) end'
      else
        cat
      fi
    }
    if [ -n "{{run_id}}" ]; then
      docker compose logs -f --no-log-prefix host | grep --line-buffered -- "{{run_id}}" | pipe
    else
      docker compose logs -f --no-log-prefix host | pipe
    fi

# install a laptop-authorized YouTube token into the container data volume (docs/deploy.md)
youtube-token-install token=".tether/youtube-oauth-token.json":
    #!/usr/bin/env bash
    set -euo pipefail
    test -f "{{token}}" || { echo 'no token at {{token}}; run `just youtube-auth` first' >&2; exit 1; }
    docker compose exec -T host mkdir -p /data/youtube
    docker compose cp "{{token}}" host:/data/youtube/token.json
    echo "installed at /data/youtube/token.json; pick it up with: docker compose restart host"

# Build the Android capture client's debug APKs (apps/capture-android: phone
# `app` + Wear OS `wear`) and run the shared `core` module's JVM unit tests.
# Not part of the JS/Python validation gate; needs a local Android SDK + a
# machine-local apps/capture-android/local.properties (see that app's
# README). Uses the pinned Gradle wrapper (AGP 8.7 is incompatible with
# system Gradle 9.6); picks a JDK 17 if one is installed (AGP rejects the
# very newest JDKs).
android-build:
    #!/usr/bin/env bash
    set -euo pipefail
    for jdk in /usr/lib/jvm/java-17-openjdk /usr/lib/jvm/java-21-openjdk; do
      if [ -x "$jdk/bin/java" ]; then export JAVA_HOME="$jdk"; break; fi
    done
    cd apps/capture-android && ./gradlew :app:assembleDebug :wear:assembleDebug :core:test
