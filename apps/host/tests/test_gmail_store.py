"""Compatibility tests for persisted Gmail synchronization state."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from snekql.sqlite import Config, Database
from snektest import assert_eq, fixture, load_fixture, test

from tether.gmail_store import (
    GMAIL_WATERMARK_KEY,
    create_gmail_schema,
    read_sync_watermark,
    write_sync_watermark,
)


@fixture
async def gmail_database() -> AsyncGenerator[Database]:
    """Own an initialized database for one schema compatibility test."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    yield database
    await database.close()


@test()
async def schema_upgrade_preserves_an_existing_sync_watermark() -> None:
    """Reapplying migrations leaves an existing Gmail cursor intact."""
    database = await load_fixture(gmail_database())
    watermark = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    await create_gmail_schema(database)
    await write_sync_watermark(database, GMAIL_WATERMARK_KEY, watermark)

    await create_gmail_schema(database)

    assert_eq(await read_sync_watermark(database, GMAIL_WATERMARK_KEY), watermark)
