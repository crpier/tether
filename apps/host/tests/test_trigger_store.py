"""Behavior tests for canonical Scheduled trigger persistence."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import assert_eq, fixture, load_fixture, test

from tether.trigger_store import ScheduledTrigger, create_trigger_schema

_LEGACY_TRIGGER_MIGRATIONS = {
    "005_create_scheduled_trigger": (
        'CREATE TABLE "scheduled_trigger" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "recurrence" TEXT NOT NULL, '
        '"action_kind" TEXT NOT NULL, "payload" TEXT NOT NULL, '
        '"timezone" TEXT NOT NULL, "wall_time" TEXT, "weekday" INTEGER, '
        '"next_fire_at" TEXT NOT NULL, "status" TEXT NOT NULL, '
        '"claimed_at" TEXT, "attempts" INTEGER NOT NULL, '
        '"next_attempt_at" TEXT, "last_error" TEXT, '
        '"version" INTEGER NOT NULL, "created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"deleted_at" TEXT) STRICT'
    ),
    "005_create_index_ix_scheduled_trigger_status_next_fire_at": (
        'CREATE INDEX "ix_scheduled_trigger_status_next_fire_at" '
        'ON "scheduled_trigger" ("status", "next_fire_at")'
    ),
}


@fixture
async def existing_trigger_database() -> AsyncGenerator[Database]:
    """Create a database carrying the original Scheduled trigger migrations."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate(_LEGACY_TRIGGER_MIGRATIONS)
    yield database
    await database.close()


async def insert_daily_trigger(database: Database) -> ScheduledTrigger[Fetched]:
    """Insert one current-shape daily trigger for schema behavior assertions."""
    async with database.transaction() as transaction:
        return await transaction.execute(
            insert(
                ScheduledTrigger(
                    recurrence="daily",
                    action_kind="message",
                    payload="stand up",
                    timezone="UTC",
                    wall_time="09:00",
                    next_fire_at=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
                    status="active",
                )
            ).returning()
        )


@test()
async def fresh_schema_persists_the_current_trigger_shape() -> None:
    """A fresh database stores recurrence and scheduler lifecycle columns."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_trigger_schema(database)

    trigger = await insert_daily_trigger(database)

    assert_eq(trigger.recurrence, "daily")
    assert_eq(trigger.attempts, 0)
    await database.close()


@test()
async def replaying_schema_on_an_existing_database_preserves_rows() -> None:
    """Recognized migration names leave existing Scheduled triggers untouched."""
    database = await load_fixture(existing_trigger_database())
    trigger = await insert_daily_trigger(database)

    await create_trigger_schema(database)

    async with database.transaction() as transaction:
        persisted = await transaction.fetch_one(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
        )
    assert_eq(persisted.payload, "stand up")
