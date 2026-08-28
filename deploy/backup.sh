#!/usr/bin/env bash
# Nightly backup: both SQLite sources + KB Markdown/sessions + .env -> restic -> B2.
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

copy_kb_source_data() {
    mkdir "${workdir}/kb"
    compose exec -T host python3 -c '
import sys
import tarfile
from pathlib import Path

root = Path(sys.argv[1])
memory_path = root / "memory"
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    if memory_path.is_dir():
        for markdown_path in sorted(memory_path.rglob("*.md")):
            relative_path = markdown_path.relative_to(memory_path)
            if (
                markdown_path.is_file()
                and not markdown_path.is_symlink()
                and all(not part.startswith((".", "~")) for part in relative_path.parts)
            ):
                archive.add(markdown_path, arcname=str(markdown_path.relative_to(root)))
    sessions_path = root / "pi-sessions"
    if sessions_path.is_dir():
        archive.add(sessions_path, arcname=sessions_path.name)
    uploads_path = root / "uploads"
    if uploads_path.is_dir():
        for upload_path in sorted(uploads_path.iterdir()):
            if upload_path.is_file() and not upload_path.is_symlink():
                archive.add(upload_path, arcname=str(upload_path.relative_to(root)))
' /data/kb | tar -C "${workdir}/kb" -xf -
}

curl --fail --silent --show-error --max-time 10 "${ping_url}/start" >/dev/null

# 1. SQLite: independently VACUUM INTO both source-of-truth databases inside
# the live container, then copy the consistent snapshots into one backup set.
snapshot_database "/data/tether.sqlite3" "${tether_container_snapshot}" "tether.sqlite3"
snapshot_database "/data/telemetry.sqlite3" "${telemetry_container_snapshot}" "telemetry.sqlite3"

# 2. KB source data. Derived Lance indexes are rebuildable and can exceed the
# host's tmpfs, so stream only Markdown, Message attachments, and retained pi
# sessions into staging.
copy_kb_source_data

# 3. .env: the app secrets, so a total-loss recovery doesn't depend on
# remembering what was in it (1Password is still the primary source of truth).
cp "${env_file}" "${workdir}/env"

restic backup "${workdir}" --tag tether --host tether-vm
restic forget \
    --host tether-vm \
    --tag tether \
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
