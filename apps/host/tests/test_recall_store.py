"""Compatibility tests for persisted Recall state."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, fixture, load_fixture, test

from tether.recall_store import StudyItem, create_recall_schema


@fixture
async def recall_database() -> AsyncGenerator[Database]:
    """An isolated database owned by one schema compatibility test."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    yield database
    await database.close()


@test()
async def schema_upgrade_preserves_an_existing_study_item() -> None:
    """Reapplying Recall migrations leaves an existing study item unchanged."""
    database = await load_fixture(recall_database())
    created_at = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    await create_recall_schema(database)
    async with database.transaction() as transaction:
        stored = await transaction.execute(
            insert(
                StudyItem(
                    memory_id=UUID("0198b996-633c-7000-8000-000000000001"),
                    source_video_id="video-1",
                    source_title="Async IO",
                    state="studying",
                    created_at=created_at,
                    updated_at=created_at,
                )
            ).returning()
        )

    await create_recall_schema(database)

    async with database.transaction() as transaction:
        reloaded = await transaction.fetch_one(
            select(StudyItem).where(StudyItem.id.eq(stored.id))
        )
    assert_eq(reloaded, stored)
