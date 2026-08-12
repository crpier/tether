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
        _ = (app / ".env").write_text("TETHER_APP_PASSWORD=secret\n")

        _write_executable(
            bin_dir / "docker",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_LOG_DIR}/docker"
case "${FAKE_FAILURE_MODE:-}:$*" in
    tether-snapshot:*"python3 - /data/tether.sqlite3"* | telemetry-snapshot:*"python3 - /data/telemetry.sqlite3"*) exit 42 ;;
    tether-copy:*"cp host:"*"-tether.sqlite3"* | telemetry-copy:*"cp host:"*"-telemetry.sqlite3"*) exit 42 ;;
esac
if [[ "$*" == *"exec -T host python3 -c"*"/data/kb"* ]]; then
    fixture_dir="$(mktemp -d)"
    trap 'rm -rf "${fixture_dir}"' EXIT
    mkdir -p \
        "${fixture_dir}/pi-sessions" \
        "${fixture_dir}/index" \
        "${fixture_dir}/transcript-index" \
        "${fixture_dir}/bucket-item-index"
    printf 'memory\n' > "${fixture_dir}/memory.md"
    printf 'session\n' > "${fixture_dir}/pi-sessions/session.jsonl"
    printf 'derived\n' > "${fixture_dir}/index/chunk.lance"
    printf 'derived\n' > "${fixture_dir}/transcript-index/chunk.lance"
    printf 'derived\n' > "${fixture_dir}/bucket-item-index/chunk.lance"
    python3 -c "${@: -2:1}" "${fixture_dir}"
    exit 0
fi
if [[ "$*" == *" cp host:"* ]]; then
    source_path="${@: -2:1}"
    destination="${@: -1}"
    if [[ "${source_path}" == *"/kb" ]]; then
        mkdir -p \
            "${destination}/pi-sessions" \
            "${destination}/index" \
            "${destination}/transcript-index" \
            "${destination}/bucket-item-index"
        printf 'memory\n' > "${destination}/memory.md"
        printf 'session\n' > "${destination}/pi-sessions/session.jsonl"
        printf 'derived\n' > "${destination}/index/chunk.lance"
        printf 'derived\n' > "${destination}/transcript-index/chunk.lance"
        printf 'derived\n' > "${destination}/bucket-item-index/chunk.lance"
    else
        printf 'consistent snapshot\n' > "${destination}"
    fi
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
            "kb/memory.md",
            "kb/pi-sessions/session.jsonl",
            "telemetry.sqlite3",
            "tether.sqlite3",
        ],
    )


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
