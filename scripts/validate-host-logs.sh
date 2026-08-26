#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

runtime_dir="$(mktemp -d)"
log_file="$runtime_dir/tether.log"
server_pid=""

cleanup() {
    if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    rm -rf "$runtime_dir"
}
trap cleanup EXIT

port="$(python - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
base_url="http://127.0.0.1:$port"
log_level="${TETHER_LOGGING_LEVEL:-INFO}"

echo "Starting Tether host on $base_url with TETHER_LOGGING_LEVEL=$log_level"
TETHER_DEPENDENCY_PROFILE=local \
TETHER_LOCAL_DATA_ROOT="$runtime_dir/local" \
TETHER_APP_PASSWORD=log-smoke-password \
TETHER_SESSION_SECRET=log-smoke-session-secret \
TETHER_STT_API_KEY=local \
TETHER_TTS_API_KEY=local \
TETHER_LOGGING_LEVEL="$log_level" \
TETHER_HOST=127.0.0.1 \
TETHER_PORT="$port" \
TETHER_RELOAD=false \
PI_CODING_AGENT_DIR="$runtime_dir/pi-agent" \
uv --project apps/host run python -m tether >"$log_file" 2>&1 &
server_pid="$!"

python - "$base_url" "$log_file" <<'PY'
import json
import sys
import time
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

base_url = sys.argv[1]
log_file = sys.argv[2]
opener = build_opener(HTTPCookieProcessor(CookieJar()))


def request(method: str, path: str, body: dict[str, object] | None = None) -> object:
    encoded = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"} if body is not None else {}
    with opener.open(
        Request(f"{base_url}{path}", data=encoded, headers=headers, method=method),
        timeout=5,
    ) as response:
        raw = response.read().decode()
        return None if not raw else json.loads(raw)

for _ in range(100):
    try:
        request("GET", "/openapi.json")
        break
    except URLError:
        time.sleep(0.1)
else:
    print("Server did not become ready. Logs:", file=sys.stderr)
    print(open(log_file).read(), file=sys.stderr)
    raise SystemExit(1)

print("\nRequests:")
print("POST /api/auth/login")
print(request("POST", "/api/auth/login", {"password": "log-smoke-password"}))
print("POST /api/conversations")
conversation = request("POST", "/api/conversations", {})
print(conversation)
assert isinstance(conversation, dict)
print("GET /api/conversations")
print(request("GET", "/api/conversations"))
print("GET /api/memory-topics")
print(request("GET", "/api/memory-topics"))
print("GET /api/todos")
print(request("GET", "/api/todos"))
PY

echo ""
echo "Structured stdout logs:"
echo "-----------------------"
cat "$log_file"
