"""Compatibility tests for persisted Readwise synchronization state."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from snekql.sqlite import Config, Database
from snektest import assert_eq, fixture, load_fixture, test

from tether.readwise_store import (
    HIGHLIGHTS_WATERMARK_KEY,
    create_readwise_schema,
    read_sync_watermark,
    write_sync_watermark,
)


@fixture
async def readwise_database() -> AsyncGenerator[Database]:
    """An initialized database owned by one schema compatibility test."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    yield database
    await database.close()


@test()
async def schema_upgrade_preserves_an_existing_sync_cursor() -> None:
    """Reapplying schema migrations leaves existing synchronization state intact."""
    database = await load_fixture(readwise_database())
    watermark = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    await create_readwise_schema(database)
    await write_sync_watermark(database, HIGHLIGHTS_WATERMARK_KEY, watermark)

    await create_readwise_schema(database)

    assert_eq(await read_sync_watermark(database, HIGHLIGHTS_WATERMARK_KEY), watermark)
