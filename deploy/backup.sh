#!/usr/bin/env bash
# Nightly backup: Tether SQLite, Open WebUI, and .env -> restic -> B2.
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
open_webui_stopped=false
open_webui_volume_name=""

compose() {
    docker compose --project-directory "${app_dir}" -f "${compose_file}" --env-file "${env_file}" "$@"
}

restart_open_webui() {
    if [ "${open_webui_stopped}" = true ]; then
        if compose start open-webui; then
            open_webui_stopped=false
        else
            return 1
        fi
    fi
}

cleanup() {
    trap - ERR
    set +e
    restart_open_webui
    compose exec -T host rm -f \
        "${tether_container_snapshot}" \
        "${telemetry_container_snapshot}" >/dev/null 2>&1
    rm -rf "${workdir}"
}
trap cleanup EXIT

on_error() {
    local exit_code=$?
    trap - ERR
    set +e
    restart_open_webui
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

stop_open_webui_for_backup() {
    local container_id

    container_id="$(compose ps -q open-webui)"
    if [ -z "${container_id}" ] || [[ "${container_id}" == *$'\n'* ]]; then
        echo "backup.sh: expected one running open-webui container" >&2
        return 1
    fi

    open_webui_volume_name="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' "${container_id}")"
    if [ -z "${open_webui_volume_name}" ] || [[ "${open_webui_volume_name}" == *$'\n'* ]]; then
        echo "backup.sh: open-webui data volume was not found" >&2
        return 1
    fi

    open_webui_stopped=true
    compose stop open-webui
}

archive_open_webui_data() {
    docker run --rm \
        --network none \
        --read-only \
        --mount "type=volume,src=${open_webui_volume_name},dst=/source,readonly" \
        --mount "type=bind,src=${workdir},dst=/backup" \
        python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17 \
        python3 -c 'import tarfile

with tarfile.open("/backup/open-webui-data.tar", mode="w") as archive:
    archive.add("/source", arcname=".")
'
}

curl --fail --silent --show-error --max-time 10 "${ping_url}/start" >/dev/null

# 1. Quiesce assistant tool mutations before capturing any related state. The
# traps restart Open WebUI if a later snapshot or archive command fails.
stop_open_webui_for_backup

# 2. SQLite: independently VACUUM INTO both source-of-truth databases inside
# the host container, then copy the consistent snapshots into one backup set.
snapshot_database "/data/tether.sqlite3" "${tether_container_snapshot}" "tether.sqlite3"
snapshot_database "/data/telemetry.sqlite3" "${telemetry_container_snapshot}" "telemetry.sqlite3"

# 3. Archive Open WebUI's actual Compose volume through a read-only mount while
# it remains stopped, then restore assistant availability.
archive_open_webui_data
restart_open_webui

# 4. .env: the app secrets, so a total-loss recovery doesn't depend on
# remembering what was in it (1Password is still the primary source of truth).
cp "${env_file}" "${workdir}/env"

restic backup "${workdir}" --tag tether-open-webui --host tether-vm
restic forget \
    --host tether-vm \
    --tag tether-open-webui \
    --group-by host,tags \
    --keep-daily 7 \
    --keep-weekly 4 \
    --prune

# Success means the backup completed and no temporary snapshots remain. The EXIT
# trap repeats this best-effort if any earlier command fails.
compose exec -T host rm -f \
    "${tether_container_snapshot}" \
    "${telemetry_container_snapshot}"
rm -rf "${workdir}"

curl --fail --silent --show-error --max-time 10 "${ping_url}" >/dev/null

echo "backup.sh: done"
