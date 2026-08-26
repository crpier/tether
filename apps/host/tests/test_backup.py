"""Production backup script behavior tests."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

from snektest import Param, assert_eq, assert_false, assert_in, assert_true, test

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKUP_SCRIPT = PROJECT_ROOT / "deploy" / "backup.sh"


@dataclass(frozen=True)
class BackupRun:
    result: subprocess.CompletedProcess[str]
    archive_manifest: list[str]
    manifest: list[str]
    docker_log: str
    restic_log: str
    curl_log: str


def _write_executable(path: Path, body: str) -> None:
    _ = path.write_text(body)
    path.chmod(0o755)


def _read_if_present(path: Path) -> str:
    return path.read_text() if path.exists() else ""


FailureMode = Literal[
    "open-webui-archive",
    "tether-snapshot",
    "telemetry-snapshot",
    "tether-copy",
    "telemetry-copy",
]


def _run_backup(*, failure_mode: FailureMode | None = None) -> BackupRun:
    with TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        app = root / "app"
        bin_dir = root / "bin"
        log_dir = root / "logs"
        app.mkdir()
        bin_dir.mkdir()
        log_dir.mkdir()
        _ = (app / "compose.yaml").write_text("services: {}\n")
        _ = (app / ".env").write_text(
            "".join(
                (
                    "TETHER_API_TOKEN=capture-secret\n",
                    "TETHER_OPEN_WEBUI_TOKEN=tool-secret\n",
                    "WEBUI_SECRET_KEY=webui-secret\n",
                    "WEBUI_URL=http://127.0.0.1:3000\n",
                )
            )
        )

        _write_executable(
            bin_dir / "docker",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_LOG_DIR}/docker"
case "${FAKE_FAILURE_MODE:-}:$*" in
    open-webui-archive:*"run --rm"*"python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"*) exit 42 ;;
    tether-snapshot:*"python3 - /data/tether.sqlite3"* | telemetry-snapshot:*"python3 - /data/telemetry.sqlite3"*) exit 42 ;;
    tether-copy:*"cp host:"*"-tether.sqlite3"* | telemetry-copy:*"cp host:"*"-telemetry.sqlite3"*) exit 42 ;;
esac
if [[ "${1:-}" == "compose" && "$*" == *" ps -q open-webui" ]]; then
    printf 'fake-open-webui-container\n'
    exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
    printf 'actual-open-webui-volume\n'
    exit 0
fi
if [[ "${1:-}" == "run" && "$*" == *"python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"* ]]; then
    backup_dir=""
    for argument in "$@"; do
        if [[ "${argument}" == type=bind,src=*,dst=/backup ]]; then
            backup_dir="${argument#type=bind,src=}"
            backup_dir="${backup_dir%,dst=/backup}"
        fi
    done
    test -n "${backup_dir}"
    fixture_dir="$(mktemp -d)"
    trap 'rm -rf "${fixture_dir}"' EXIT
    mkdir -p "${fixture_dir}/uploads"
    printf 'database\n' > "${fixture_dir}/webui.db"
    printf 'upload\n' > "${fixture_dir}/uploads/file.txt"
    tar -cf "${backup_dir}/open-webui-data.tar" \
        -C "${fixture_dir}" webui.db uploads/file.txt
    exit 0
fi
if [[ "$*" == *" cp host:"* ]]; then
    source_path="${@: -2:1}"
    destination="${@: -1}"
    printf 'consistent snapshot\n' > "${destination}"
fi
""",
        )
        _write_executable(
            bin_dir / "restic",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_LOG_DIR}/restic"
if [[ "${1:-}" == "backup" ]]; then
    find "$2" -type f -printf '%P\n' | sort > "${FAKE_LOG_DIR}/manifest"
    tar -tf "$2/open-webui-data.tar" > "${FAKE_LOG_DIR}/archive-manifest"
fi
""",
        )
        _write_executable(
            bin_dir / "curl",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_LOG_DIR}/curl"
""",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{bin_dir}:{environment['PATH']}",
                "FAKE_LOG_DIR": str(log_dir),
                "FAKE_FAILURE_MODE": failure_mode or "",
                "TETHER_APP_DIR": str(app),
                "RESTIC_REPOSITORY": "fake:repository",
                "RESTIC_PASSWORD": "secret",
                "B2_ACCOUNT_ID": "account",
                "B2_ACCOUNT_KEY": "key",
                "HEALTHCHECKS_PING_URL": "https://health.example/id",
            }
        )

        result = subprocess.run(
            [str(BACKUP_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        manifest_path = log_dir / "manifest"
        return BackupRun(
            result=result,
            archive_manifest=_read_if_present(
                log_dir / "archive-manifest"
            ).splitlines(),
            manifest=manifest_path.read_text().splitlines()
            if manifest_path.exists()
            else [],
            docker_log=_read_if_present(log_dir / "docker"),
            restic_log=_read_if_present(log_dir / "restic"),
            curl_log=_read_if_present(log_dir / "curl"),
        )


@test(mark="slow")
def test_backup_snapshots_both_sqlite_sources_of_truth() -> None:
    run = _run_backup()

    assert_eq(run.result.returncode, 0)
    assert_eq(
        run.manifest,
        [
            "env",
            "open-webui-data.tar",
            "telemetry.sqlite3",
            "tether.sqlite3",
        ],
    )


@test(mark="slow")
def test_backup_archives_the_open_webui_compose_volume_read_only() -> None:
    """The stopped service's resolved data volume is archived without mutation."""
    run = _run_backup()

    assert_eq(run.result.returncode, 0)
    assert_eq(run.archive_manifest, ["webui.db", "uploads/file.txt"])
    assert_in("ps -q open-webui", run.docker_log)
    assert_in("inspect --format", run.docker_log)
    assert_in("fake-open-webui-container", run.docker_log)
    assert_in("stop open-webui", run.docker_log)
    assert_in(
        "type=volume,src=actual-open-webui-volume,dst=/source,readonly",
        run.docker_log,
    )
    assert_in(
        "python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17",
        run.docker_log,
    )
    assert_in("start open-webui", run.docker_log)
    assert_false("stop host" in run.docker_log)
    assert_false("volume rm" in run.docker_log)
    archive_position = run.docker_log.index("python:3.12-slim@sha256:")
    restart_position = run.docker_log.index("start open-webui")
    assert_true(archive_position < restart_position)


@test(mark="slow")
def test_backup_restarts_open_webui_when_its_archive_fails() -> None:
    """The error trap restarts Open WebUI before preserving backup failure."""
    run = _run_backup(failure_mode="open-webui-archive")

    assert_eq(run.result.returncode, 42)
    assert_eq(run.restic_log, "")
    assert_in("start open-webui", run.docker_log)
    assert_in("https://health.example/id/fail", run.curl_log)
    archive_position = run.docker_log.index("python:3.12-slim@sha256:")
    restart_position = run.docker_log.index("start open-webui")
    assert_true(archive_position < restart_position)


@test(
    [
        Param(value="tether-snapshot", name="tether_snapshot"),
        Param(value="telemetry-snapshot", name="telemetry_snapshot"),
        Param(value="tether-copy", name="tether_copy"),
        Param(value="telemetry-copy", name="telemetry_copy"),
    ],
    mark="slow",
)
def test_backup_fails_as_a_whole_when_either_database_is_incomplete(
    failure_mode: str,
) -> None:
    run = _run_backup(failure_mode=cast("FailureMode", failure_mode))

    assert_eq(run.result.returncode, 42)
    assert_eq(run.restic_log, "")
    assert_in("https://health.example/id/fail", run.curl_log)
    assert_false(
        any(
            "https://health.example/id" in line.split()
            for line in run.curl_log.splitlines()
        )
    )
    cleanup_lines = [
        line for line in run.docker_log.splitlines() if " exec -T host rm -f " in line
    ]
    assert_eq(len(cleanup_lines), 1)
    assert_true("-tether.sqlite3" in cleanup_lines[0])
    assert_true("-telemetry.sqlite3" in cleanup_lines[0])
