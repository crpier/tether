"""Public CLI behavior for the one-shot linked-Todo cleanup."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, assert_false, assert_in, assert_true, test

HOST_ROOT = Path(__file__).resolve().parents[1]


def _seed_database(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            """CREATE TABLE todo (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                condition TEXT,
                trigger_id TEXT,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        _ = connection.executemany(
            "INSERT INTO todo VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "0198f000-0000-7000-8000-000000000001",
                    "Book the dentist",
                    "active",
                    None,
                    "trigger-1",
                    3,
                    "2026-08-01T10:00:00Z",
                    "2026-08-02T10:00:00Z",
                ),
                (
                    "0198f000-0000-7000-8000-000000000002",
                    "Renew the passport",
                    "active",
                    "after photos arrive",
                    "trigger-2",
                    5,
                    "2026-08-03T10:00:00Z",
                    "2026-08-04T10:00:00Z",
                ),
                (
                    "0198f000-0000-7000-8000-000000000003",
                    "Buy coffee",
                    "completed",
                    None,
                    None,
                    2,
                    "2026-08-05T10:00:00Z",
                    "2026-08-06T10:00:00Z",
                ),
            ],
        )
        _ = connection.execute(
            "CREATE TABLE scheduled_trigger (id TEXT PRIMARY KEY, prompt TEXT NOT NULL)"
        )
        _ = connection.execute(
            "INSERT INTO scheduled_trigger VALUES ('trigger-1', 'Call dentist')"
        )
        connection.commit()


def _run_cleanup(
    database_path: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["TETHER_DATABASE_PATH"] = str(database_path)
    return subprocess.run(
        [sys.executable, "-m", "tether.cleanup_linked_todos", *arguments],
        cwd=HOST_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@test()
def default_run_reports_every_linked_todo_without_changing_the_database() -> None:
    """No confirmation means a complete, human-readable dry run."""
    with TemporaryDirectory() as raw_directory:
        database_path = Path(raw_directory) / "tether.sqlite3"
        _seed_database(database_path)

        completed = _run_cleanup(database_path)

        assert_eq(completed.returncode, 0)
        assert_in("Linked Todos (2):", completed.stdout)
        assert_in(
            "- 0198f000-0000-7000-8000-000000000001: Book the dentist [trigger: trigger-1]",
            completed.stdout,
        )
        assert_in(
            "- 0198f000-0000-7000-8000-000000000002: Renew the passport [trigger: trigger-2]",
            completed.stdout,
        )
        assert_false("Buy coffee" in completed.stdout)
        assert_in("Dry run: no changes made.", completed.stdout)
        with closing(sqlite3.connect(database_path)) as connection:
            trigger_ids = connection.execute(
                "SELECT trigger_id FROM todo ORDER BY id"
            ).fetchall()
        assert_eq(trigger_ids, [("trigger-1",), ("trigger-2",), (None,)])


@test()
def confirm_clears_only_the_reported_todo_trigger_links() -> None:
    """Explicit confirmation unlinks Todos without touching any other state."""
    with TemporaryDirectory() as raw_directory:
        database_path = Path(raw_directory) / "tether.sqlite3"
        _seed_database(database_path)

        completed = _run_cleanup(database_path, "--confirm")

        assert_eq(completed.returncode, 0)
        assert_in("Linked Todos (2):", completed.stdout)
        assert_in("Cleared trigger links from 2 Todos.", completed.stdout)
        with closing(sqlite3.connect(database_path)) as connection:
            todos = connection.execute("SELECT * FROM todo ORDER BY id").fetchall()
            triggers = connection.execute(
                "SELECT * FROM scheduled_trigger ORDER BY id"
            ).fetchall()
        assert_eq(
            todos,
            [
                (
                    "0198f000-0000-7000-8000-000000000001",
                    "Book the dentist",
                    "active",
                    None,
                    None,
                    3,
                    "2026-08-01T10:00:00Z",
                    "2026-08-02T10:00:00Z",
                ),
                (
                    "0198f000-0000-7000-8000-000000000002",
                    "Renew the passport",
                    "active",
                    "after photos arrive",
                    None,
                    5,
                    "2026-08-03T10:00:00Z",
                    "2026-08-04T10:00:00Z",
                ),
                (
                    "0198f000-0000-7000-8000-000000000003",
                    "Buy coffee",
                    "completed",
                    None,
                    None,
                    2,
                    "2026-08-05T10:00:00Z",
                    "2026-08-06T10:00:00Z",
                ),
            ],
        )
        assert_eq(triggers, [("trigger-1", "Call dentist")])


@test()
def confirmed_cleanup_is_idempotent() -> None:
    """Re-running after cleanup succeeds and finds no remaining links."""
    with TemporaryDirectory() as raw_directory:
        database_path = Path(raw_directory) / "tether.sqlite3"
        _seed_database(database_path)
        first = _run_cleanup(database_path, "--confirm")

        second = _run_cleanup(database_path, "--confirm")

        assert_eq(first.returncode, 0)
        assert_eq(second.returncode, 0)
        assert_in("No linked Todos found.", second.stdout)
        assert_true("Cleared trigger links" not in second.stdout)
