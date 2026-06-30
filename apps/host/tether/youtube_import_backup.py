"""`python -m tether.youtube_import_backup <state.db>`: absorb an AW backup.

Wired to the `just youtube-import-backup` recipe. Opens Tether's own database
(the `TETHER_DATABASE_PATH` the host uses), reads the active-workbench backup at
the given path through `SqliteLikesBackupReader`, and imports its liked videos
and transcripts into the ingested corpus via `import_backup`. It never calls
YouTube, is idempotent (re-running updates rather than duplicates), and prints a
summary. Pass `--dry-run` to preview the counts without writing.

It reads only its own small setting (`TETHER_DATABASE_PATH`) rather than the full
host settings, so importing a backup does not require the app password or session
secret to be set.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database

from tether.logging import Logger
from tether.youtube import create_youtube_schema
from tether.youtube_import import (
    ImportReport,
    SqliteLikesBackupReader,
    import_backup,
)


class ImportSettings(BaseSettings):
    """The `TETHER_` subset the backup import needs (just the database path)."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    database_path: Path = Path(".tether/tether.sqlite3")


async def _run(
    *, backup_path: Path, database_path: Path, dry_run: bool, logger: Logger
) -> ImportReport:
    async with await Database.initialize(
        backend=Config(database=database_path)
    ) as database:
        await create_youtube_schema(database)
        reader = SqliteLikesBackupReader(backup_path)
        return await import_backup(database, reader, dry_run=dry_run, logger=logger)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tether.youtube_import_backup",
        description="Import an active-workbench YouTube backup into Tether.",
    )
    _ = parser.add_argument(
        "backup_path",
        type=Path,
        help="path to the active-workbench state.db to import",
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing anything",
    )
    return parser.parse_args()


def main() -> None:
    """Run the backup import from CLI arguments and environment settings."""
    args = _parse_args()
    backup_path: Path = args.backup_path
    dry_run: bool = args.dry_run
    if not backup_path.exists():
        print(f"Backup database not found: {backup_path}")
        raise SystemExit(1)
    settings = ImportSettings()
    logger = structlog.stdlib.get_logger("tether.youtube_import_backup")
    report = asyncio.run(
        _run(
            backup_path=backup_path,
            database_path=settings.database_path,
            dry_run=dry_run,
            logger=logger,
        )
    )
    prefix = (
        "Dry run — nothing written. Would import:" if report.dry_run else "Imported:"
    )
    print(prefix)
    print(f"  videos inserted:     {report.videos_inserted}")
    print(f"  videos updated:      {report.videos_updated}")
    print(f"  orphan videos made:  {report.orphans_created}")
    print(f"  transcripts imported:{report.transcripts_imported}")
    print(f"  rows skipped:        {report.skipped}")


if __name__ == "__main__":
    main()
