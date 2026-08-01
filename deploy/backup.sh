#!/usr/bin/env bash
# Nightly backup: safe snapshots of both SQLite sources + kb_root + .env -> restic -> B2.
# Run by the `tether-backup` systemd timer (docs/deployment.md#backups); safe to
# run by hand too: `sudo -E deploy/backup.sh` (needs restic.env sourced/exported
# and docker compose access).
#
# Required env (normally supplied by systemd's EnvironmentFile=restic.env, see
# deploy/restic.env.example): RESTIC_REPOSITORY, RESTIC_PASSWORD, B2_ACCOUNT_ID,
# B2_ACCOUNT_KEY, HEALTHCHECKS_PING_URL. Optional: TETHER_APP_DIR (default
# /srv/tether).
set -Eeuo pipefail

app_dir="${TETHER_APP_DIR:-/srv/tether}"
ping_url="${HEALTHCHECKS_PING_URL:?HEALTHCHECKS_PING_URL must be set}"
compose_file="${app_dir}/compose.yaml"
env_file="${app_dir}/.env"

for var in RESTIC_REPOSITORY RESTIC_PASSWORD B2_ACCOUNT_ID B2_ACCOUNT_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "backup.sh: ${var} must be set" >&2
        exit 1
    fi
done
export RESTIC_REPOSITORY RESTIC_PASSWORD B2_ACCOUNT_ID B2_ACCOUNT_KEY

workdir="$(mktemp -d)"
snapshot_suffix="${workdir##*/}"
tether_container_snapshot="/data/.tether-backup-${snapshot_suffix}-tether.sqlite3"
telemetry_container_snapshot="/data/.tether-backup-${snapshot_suffix}-telemetry.sqlite3"

compose() {
    docker compose --project-directory "${app_dir}" -f "${compose_file}" --env-file "${env_file}" "$@"
}

cleanup() {
    trap - ERR
    set +e
    compose exec -T host rm -f \
        "${tether_container_snapshot}" \
        "${telemetry_container_snapshot}" >/dev/null 2>&1
    rm -rf "${workdir}"
}
trap cleanup EXIT

on_error() {
    local exit_code=$?
    curl --fail --silent --show-error --max-time 10 "${ping_url}/fail" \
        --data-raw "backup.sh failed (exit ${exit_code}); see journalctl -u tether-backup" \
        >/dev/null 2>&1 || true
    exit "${exit_code}"
}
trap on_error ERR

snapshot_database() {
    local source_path=$1
    local container_snapshot=$2
    local output_name=$3

    # mode=rw prevents a missing source from being silently created as an empty DB.
    compose exec -T host python3 - "${source_path}" "${container_snapshot}" <<'PY'
import sqlite3
import sys
from contextlib import closing

source_path, snapshot_path = sys.argv[1:]
with closing(sqlite3.connect(f"file:{source_path}?mode=rw", uri=True)) as connection:
    connection.execute("VACUUM INTO ?", (snapshot_path,))
PY
    compose cp "host:${container_snapshot}" "${workdir}/${output_name}"
}

curl --fail --silent --show-error --max-time 10 "${ping_url}/start" >/dev/null

# 1. SQLite: independently VACUUM INTO both source-of-truth databases inside
# the live container, then copy the consistent snapshots into one backup set.
snapshot_database "/data/tether.sqlite3" "${tether_container_snapshot}" "tether.sqlite3"
snapshot_database "/data/telemetry.sqlite3" "${telemetry_container_snapshot}" "telemetry.sqlite3"

# 2. kb_root, copied whole. It currently co-locates derived indexes and pi
# sessions with Markdown, so those files are included too (docs/deployment.md).
compose cp "host:/data/kb" "${workdir}/kb"

# 3. .env: the app secrets, so a total-loss recovery doesn't depend on
# remembering what was in it (1Password is still the primary source of truth).
cp "${env_file}" "${workdir}/env"

restic backup "${workdir}" --tag tether --host tether-vm
restic forget --keep-daily 7 --keep-weekly 4 --prune

# Success means the backup completed and no temporary snapshots remain. The EXIT
# trap repeats this best-effort if any earlier command fails.
compose exec -T host rm -f \
    "${tether_container_snapshot}" \
    "${telemetry_container_snapshot}"
rm -rf "${workdir}"

curl --fail --silent --show-error --max-time 10 "${ping_url}" >/dev/null

echo "backup.sh: done"
