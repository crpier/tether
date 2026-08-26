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
TETHER_DATABASE_PATH="$runtime_dir/tether.sqlite3" \
TETHER_TELEMETRY_DATABASE_PATH="$runtime_dir/telemetry.sqlite3" \
TETHER_API_TOKEN=log-smoke-capture-token \
TETHER_OPEN_WEBUI_TOKEN=log-smoke-open-webui-token \
TETHER_LOGGING_LEVEL="$log_level" \
TETHER_HOST=127.0.0.1 \
TETHER_PORT="$port" \
TETHER_RELOAD=false \
uv --project apps/host run python -m tether >"$log_file" 2>&1 &
server_pid="$!"

python - "$base_url" "$log_file" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

base_url = sys.argv[1]
log_file = sys.argv[2]


def request(
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    authorized: bool = False,
) -> object:
    encoded = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"} if body is not None else {}
    if authorized:
        headers["authorization"] = "Bearer log-smoke-open-webui-token"
    with urlopen(Request(f"{base_url}{path}", data=encoded, headers=headers, method=method), timeout=5) as response:
        raw = response.read().decode()
        return None if not raw else json.loads(raw)

for _ in range(100):
    try:
        request("GET", "/health")
        break
    except URLError:
        time.sleep(0.1)
else:
    print("Server did not become ready. Logs:", file=sys.stderr)
    print(open(log_file).read(), file=sys.stderr)
    raise SystemExit(1)

print("\nRequests:")
print("GET /health")
print(request("GET", "/health"))
print("GET /tools/openapi.json")
schema = request("GET", "/tools/openapi.json", authorized=True)
assert isinstance(schema, dict)
paths = schema.get("paths")
assert isinstance(paths, dict)
print({"operation_count": len(paths)})
print("POST /tools/create_todo")
print(
    request(
        "POST",
        "/tools/create_todo",
        {"action": "Verify structured host logs"},
        authorized=True,
    )
)
print("POST /tools/list_todos")
print(request("POST", "/tools/list_todos", {}, authorized=True))
PY

echo ""
echo "Structured stdout logs:"
echo "-----------------------"
cat "$log_file"
