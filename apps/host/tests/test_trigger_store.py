"""Behavior tests for canonical Scheduled trigger persistence."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry import trace
from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import assert_eq, fixture, load_fixture, test

from tether.conversation_store import create_conversation_schema
from tether.conversations import ConversationService
from tether.trigger_store import ScheduledTrigger, create_trigger_schema
from tether.triggers import TriggerService

_LEGACY_TRIGGER_ID = UUID("018f0000-0000-7000-8000-0000000000aa")

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
    await database.migrate(
        {
            "test_seed_legacy_scheduled_trigger": (
                'INSERT INTO "scheduled_trigger" '
                '("id", "recurrence", "action_kind", "payload", "timezone", '
                '"wall_time", "next_fire_at", "status", "attempts", "version") '
                f"VALUES ('{_LEGACY_TRIGGER_ID}', 'daily', 'message', 'stand up', "
                "'UTC', '09:00', '2030-01-01T09:00:00.000Z', 'active', 0, 1)"
            )
        }
    )
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
async def legacy_actions_migrate_to_main_only_for_prompts() -> None:
    """Legacy prompts gain Main while fixed messages keep a null target."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(database)
    await create_trigger_schema(database)
    main = await ConversationService(database).fetch_main_conversation()
    async with database.transaction(mode="immediate") as transaction:
        prompt = await transaction.execute(
            insert(
                ScheduledTrigger(
                    recurrence="daily",
                    action_kind="prompt",
                    payload="summarise",
                    timezone="UTC",
                    wall_time="09:00",
                    next_fire_at=datetime(2030, 1, 1, 9, tzinfo=UTC),
                    status="active",
                )
            ).returning()
        )
        message = await transaction.execute(
            insert(
                ScheduledTrigger(
                    recurrence="daily",
                    action_kind="message",
                    payload="stand up",
                    target_conversation_id=main.id,
                    timezone="UTC",
                    wall_time="10:00",
                    next_fire_at=datetime(2030, 1, 1, 10, tzinfo=UTC),
                    status="active",
                )
            ).returning()
        )
    service = TriggerService(
        database,
        tracer=trace.NoOpTracerProvider().get_tracer("test.trigger_store"),
    )

    await service.migrate_legacy_targets(main.id)
    prompt = await service.fetch(prompt.id)
    message = await service.fetch(message.id)

    assert_eq(prompt.target_conversation_id, main.id)
    assert_eq(message.target_conversation_id, None)
    await database.close()


@test()
async def replaying_schema_on_an_existing_database_preserves_rows() -> None:
    """Recognized migration names leave existing Scheduled triggers untouched."""
    database = await load_fixture(existing_trigger_database())

    await create_trigger_schema(database)

    async with database.transaction() as transaction:
        persisted = await transaction.fetch_one(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(_LEGACY_TRIGGER_ID))
        )
    assert_eq(persisted.payload, "stand up")
    assert_eq(persisted.model_profile, None)
