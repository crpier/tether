#!/usr/bin/env bash
# Real-browser smoke check for the web SPA.
#
# Boots the host and the Vite dev server on ephemeral ports (so it never
# collides with a running `just host` / `just web`), points the dev server's
# /api + /ws proxy at the ephemeral host, then drives headless Chromium through
# the unauthenticated load and post-login chat view. Fails on any console
# error, uncaught page error, 5xx response, or failed request.
set -euo pipefail

cd "$(dirname "$0")/.."

runtime_dir="$(mktemp -d)"
host_log="$runtime_dir/host.log"
web_log="$runtime_dir/web.log"
host_pid=""
web_pid=""

cleanup() {
    for pid in "$web_pid" "$host_pid"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    rm -rf "$runtime_dir"
}
trap cleanup EXIT

read -r host_port web_port < <(python - <<'PY'
import socket


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


print(free_port(), free_port())
PY
)

host_url="http://127.0.0.1:$host_port"
web_url="http://127.0.0.1:$web_port"
app_password="dev"

echo "Starting host on $host_url"
TETHER_DATABASE_PATH="$runtime_dir/tether.sqlite3" \
TETHER_KB_ROOT="$runtime_dir/kb" \
TETHER_HOST=127.0.0.1 \
TETHER_PORT="$host_port" \
TETHER_RELOAD=false \
TETHER_APP_PASSWORD="$app_password" \
TETHER_SESSION_SECRET=web-smoke-session-secret \
uv --project apps/host run python -m tether >"$host_log" 2>&1 &
host_pid="$!"

echo "Starting web dev server on $web_url"
TETHER_API_TARGET="$host_url" \
TETHER_WS_TARGET="ws://127.0.0.1:$host_port" \
bash -c "cd apps/web && exec node_modules/.bin/vite --host 127.0.0.1 --port $web_port --strictPort" \
    >"$web_log" 2>&1 &
web_pid="$!"

wait_for() {
    local name="$1" url="$2"
    for _ in $(seq 1 150); do
        if curl --silent --output /dev/null "$url"; then
            return 0
        fi
        sleep 0.2
    done
    echo "$name did not become ready at $url" >&2
    return 1
}

if ! wait_for "host" "$host_url/openapi.json"; then
    echo "--- host log ---" >&2
    cat "$host_log" >&2
    exit 1
fi
if ! wait_for "web dev server" "$web_url"; then
    echo "--- web log ---" >&2
    cat "$web_log" >&2
    exit 1
fi

echo "Running browser smoke against $web_url"
smoke_status=0
TETHER_SMOKE_WEB_URL="$web_url" \
TETHER_APP_PASSWORD="$app_password" \
TETHER_SMOKE_SCREENSHOT="$runtime_dir/smoke.png" \
node apps/web/scripts/smoke.mjs || smoke_status="$?"

if [[ "$smoke_status" -ne 0 ]]; then
    echo "--- host log (tail) ---" >&2
    tail -n 40 "$host_log" >&2 || true
    echo "--- web log (tail) ---" >&2
    tail -n 40 "$web_log" >&2 || true
fi

exit "$smoke_status"
