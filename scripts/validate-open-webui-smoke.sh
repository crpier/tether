#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
suite="$root/tests/open-webui"
project="tether-open-webui-smoke-${UID}-$$-${RANDOM}"
compose_file="$suite/compose.yaml"

export TETHER_SMOKE_CAPTURE_TOKEN="smoke-capture-token-not-valid-for-tools"
export TETHER_SMOKE_COMPOSE_FILE="$compose_file"
export TETHER_SMOKE_COMPOSE_PROJECT="$project"
export TETHER_SMOKE_HOST_IMAGE="${project}-host:local"
export TETHER_SMOKE_PROVIDER_TOKEN="smoke-provider-token"
export TETHER_SMOKE_TOOL_TOKEN="smoke-open-webui-tool-token"

compose=(docker compose -p "$project" -f "$compose_file")

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if ((status != 0)); then
    "${compose[@]}" ps >&2 || true
    "${compose[@]}" logs --no-color >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans --timeout 5 >/dev/null 2>&1 || true
  docker image rm "$TETHER_SMOKE_HOST_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$suite/__pycache__"
  exit "$status"
}
trap cleanup EXIT INT TERM

docker compose -p "$project" -f "$compose_file" config --quiet
python -m py_compile "$suite/fake_provider.py"
npm --prefix "$suite" ci
npm --prefix "$suite" run typecheck
npm --prefix "$suite" run lint
npm --prefix "$suite" run format:check
npx --prefix "$suite" playwright install chromium

"${compose[@]}" up --detach --build --wait --wait-timeout 180

published_url() {
  service=$1
  container_port=$2
  address=$("${compose[@]}" port "$service" "$container_port")
  printf 'http://%s\n' "$address"
}

export TETHER_SMOKE_HOST_URL="$(published_url host 8000)"
export TETHER_SMOKE_PROVIDER_URL="$(published_url fake-provider 8081)"
export TETHER_SMOKE_WEBUI_URL="$(published_url open-webui 8080)"

npm --prefix "$suite" test
