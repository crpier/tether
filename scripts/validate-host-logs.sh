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
TETHER_KB_ROOT="$runtime_dir/kb" \
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


def request(method: str, path: str, body: dict[str, object] | None = None) -> object:
    encoded = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"} if body is not None else {}
    with urlopen(Request(f"{base_url}{path}", data=encoded, headers=headers, method=method), timeout=5) as response:
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
print("GET /memories?state=loose")
print(request("GET", "/memories?state=loose"))
print("POST /memories")
memory = request("POST", "/memories", {"content": "  I prefer aisle seats  "})
print(memory)
assert isinstance(memory, dict)
print(f"POST /memories/{memory['id']}/tether")
tethered = request("POST", f"/memories/{memory['id']}/tether", {"version": memory["version"]})
print(tethered)
print("GET /memories/search?q=aisle")
print(request("GET", "/memories/search?q=aisle"))
PY

echo ""
echo "Structured stdout logs:"
echo "-----------------------"
cat "$log_file"
