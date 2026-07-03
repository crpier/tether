"""CLI escape hatch for forcing a full YouTube likes backfill re-walk."""

from __future__ import annotations

import asyncio

from snekql.sqlite import Config, Database

from tether.server import HostSettings
from tether.youtube import create_youtube_schema, reset_likes_backfill_state


async def _main() -> None:
    """Reset persisted likes-backfill cursor state in the configured database."""
    settings = HostSettings()
    database = await Database.initialize(
        backend=Config(database=settings.database_path)
    )
    try:
        await create_youtube_schema(database)
        await reset_likes_backfill_state(database)
    finally:
        await database.close()


def main() -> None:
    """Run the reset command from `python -m tether.youtube_reset_backfill`."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
