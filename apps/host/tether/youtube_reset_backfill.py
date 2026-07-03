"""`python -m tether.youtube_reset_backfill`: force a full liked-history resync.

Wired to the `just youtube-reset-backfill` recipe. The background sync walks the
liked-videos history once and then leaves it settled (only the hot pages keep
refreshing) until the re-walk interval elapses. This escape hatch clears the
persisted backfill cursor and completion marker so the next sync pass re-walks
history from the tail on demand — useful after importing a large backup or when
the local corpus looks stale.

It reads only its own small setting (`TETHER_DATABASE_PATH`) rather than the full
host settings, so resetting does not require the app password or session secret to
be set. It never calls YouTube; the actual re-walk happens on the next sync pass.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from opentelemetry import trace
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database

from tether.logging import Logger
from tether.youtube import (
    DailyQuota,
    InMemoryYouTubeApi,
    YouTubeApiClient,
    YouTubeSyncService,
    create_youtube_schema,
)


class ResetSettings(BaseSettings):
    """The `TETHER_` subset the reset needs (just the database path)."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    database_path: Path = Path(".tether/tether.sqlite3")


async def _run(*, database_path: Path, logger: Logger) -> None:
    async with await Database.initialize(
        backend=Config(database=database_path)
    ) as database:
        await create_youtube_schema(database)
        # The reset only touches persisted bookkeeping, so the upstream client is a
        # throwaway fake — no YouTube call is made here.
        sync = YouTubeSyncService(
            database=database,
            client=YouTubeApiClient(
                InMemoryYouTubeApi(), DailyQuota(database, limit=0)
            ),
            tracer=trace.NoOpTracerProvider().get_tracer("tether.youtube_reset"),
        )
        await sync.reset_backfill()
        logger.info("YouTube likes backfill reset; next sync pass re-walks history")


def main() -> None:
    """Clear the backfill cursor + completion marker from environment settings."""
    logger = structlog.stdlib.get_logger("tether.youtube_reset_backfill")
    asyncio.run(_run(database_path=ResetSettings().database_path, logger=logger))
    print("Backfill reset. The next sync pass will re-walk liked history.")


if __name__ == "__main__":
    main()
