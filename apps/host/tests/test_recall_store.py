"""Compatibility tests for persisted Recall state."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, assert_in, assert_not_in, fixture, load_fixture, test

from tether.recall_store import StudyItem, create_recall_schema


@fixture
async def recall_database() -> AsyncGenerator[Database]:
    """An isolated database owned by one schema compatibility test."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    yield database
    await database.close()


@test()
async def current_study_items_own_distilled_learnings_without_a_memory_link() -> None:
    """Recall persists learning material without participating in Memory trust."""
    database = await load_fixture(recall_database())

    await create_recall_schema(database)

    async with database.transaction() as transaction:
        connection = transaction.require_connection()
        cursor = await connection.execute('PRAGMA table_info("study_item")', ())
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
    assert_in("distilled_learnings", columns)
    assert_not_in("memory_id", columns)


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
                    source_video_id="video-1",
                    source_title="Async IO",
                    distilled_learnings="Async IO multiplexes waits.",
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
