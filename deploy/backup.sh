#!/usr/bin/env bash
# Nightly backup: sqlite3 VACUUM INTO snapshot + kb_root + .env -> restic -> B2.
# Run by the `tether-backup` systemd timer (docs/deployment.md#backups); safe to
# run by hand too: `sudo -E deploy/backup.sh` (needs restic.env sourced/exported
# and docker compose access).
#
# Required env (normally supplied by systemd's EnvironmentFile=restic.env, see
# deploy/restic.env.example): RESTIC_REPOSITORY, RESTIC_PASSWORD, B2_ACCOUNT_ID,
# B2_ACCOUNT_KEY, HEALTHCHECKS_PING_URL. Optional: TETHER_APP_DIR (default
# /srv/tether).
set -euo pipefail

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
container_snapshot="/data/backup-snapshot.sqlite3"

cleanup() {
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

compose() {
    docker compose --project-directory "${app_dir}" -f "${compose_file}" --env-file "${env_file}" "$@"
}

curl --fail --silent --show-error --max-time 10 "${ping_url}/start" >/dev/null

# 1. SQLite: VACUUM INTO a fresh snapshot inside the container (defragmented,
# consistent even against a live writer), then copy it out and delete it.
compose exec -T host python3 -c "
import sqlite3
sqlite3.connect('/data/tether.sqlite3').execute(\"VACUUM INTO '${container_snapshot}'\")
"
compose cp "host:${container_snapshot}" "${workdir}/tether.sqlite3"
compose exec -T host rm -f "${container_snapshot}"

# 2. kb_root: the derived markdown KB, copied whole.
compose cp "host:/data/kb" "${workdir}/kb"

# 3. .env: the app secrets, so a total-loss recovery doesn't depend on
# remembering what was in it (1Password is still the primary source of truth).
cp "${env_file}" "${workdir}/env"

restic backup "${workdir}" --tag tether --host tether-vm
restic forget --keep-daily 7 --keep-weekly 4 --prune

curl --fail --silent --show-error --max-time 10 "${ping_url}" >/dev/null

echo "backup.sh: done"
