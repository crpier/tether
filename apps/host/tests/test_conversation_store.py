"""Conversation schema migration behavior."""

from uuid import UUID

from snekql.sqlite import Config, Database, select
from snektest import assert_eq, test

from tether.conversation_store import (
    Conversation,
    ConversationTurn,
    Message,
    create_conversation_schema,
)

_OLDEST_ID = UUID("018f0000-0000-7000-8000-000000000001")
_NEWER_ID = UUID("018f0000-0000-7000-8000-000000000002")
_UNTITLED_ID = UUID("018f0000-0000-7000-8000-000000000003")


@test()
async def migration_assigns_explicit_lifecycle_to_existing_conversations() -> None:
    """Oldest legacy row becomes Main and additional rows become archived Scoped."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate(
        {
            "003_create_conversation": (
                'CREATE TABLE "conversation" ("id" TEXT PRIMARY KEY NOT NULL, '
                '"created_at" TEXT NOT NULL, "pi_session_id" TEXT NOT NULL, '
                '"selected_model" TEXT, "title" TEXT) STRICT'
            ),
            "003_create_message": (
                'CREATE TABLE "message" ("id" TEXT PRIMARY KEY NOT NULL, '
                '"conversation_id" TEXT NOT NULL, "seq" INTEGER NOT NULL, '
                '"role" TEXT NOT NULL, "content" TEXT NOT NULL, '
                '"created_at" TEXT NOT NULL, "pi_message_id" TEXT, '
                '"tool_args" TEXT, "tool_name" TEXT, "tool_result" TEXT) STRICT'
            ),
            "test_seed_oldest_conversation": (
                'INSERT INTO "conversation" '
                '("id", "created_at", "pi_session_id", "title") '
                f"VALUES ('{_OLDEST_ID}', '2025-01-01T00:00:00.000Z', "
                "'018f0000-0000-7000-8000-000000000011', 'Legacy main')"
            ),
            "test_seed_newer_conversation": (
                'INSERT INTO "conversation" '
                '("id", "created_at", "pi_session_id", "title") '
                f"VALUES ('{_NEWER_ID}', '2025-01-02T00:00:00.000Z', "
                "'018f0000-0000-7000-8000-000000000012', 'Project Atlas')"
            ),
            "test_seed_untitled_conversation": (
                'INSERT INTO "conversation" '
                '("id", "created_at", "pi_session_id", "title") '
                f"VALUES ('{_UNTITLED_ID}', '2025-01-03T00:00:00.000Z', "
                "'018f0000-0000-7000-8000-000000000013', '  ')"
            ),
        }
    )

    await create_conversation_schema(database)

    async with database.transaction() as transaction:
        conversations = await transaction.fetch_all(
            select(Conversation).all().order_by(Conversation.created_at.asc())
        )
    await database.close()
    assert_eq(conversations[0].kind, "main")
    assert_eq(conversations[0].status, "active")
    assert_eq(conversations[0].display_name, None)
    assert_eq(conversations[1].kind, "scoped")
    assert_eq(conversations[1].status, "archived")
    assert_eq(conversations[1].display_name, "Project Atlas")
    assert_eq(conversations[1].scope_brief, "No additional scope.")
    assert_eq(conversations[2].status, "archived")
    assert_eq(conversations[2].display_name, "Imported conversation 018f0000")


@test()
async def migration_groups_historical_transcript_without_changing_roles() -> None:
    """Leading rows and each user-led group become one settled historical turn."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate(
        {
            "003_create_conversation": (
                'CREATE TABLE "conversation" ("id" TEXT PRIMARY KEY NOT NULL, '
                '"created_at" TEXT NOT NULL, "pi_session_id" TEXT NOT NULL, '
                '"selected_model" TEXT, "title" TEXT) STRICT'
            ),
            "003_create_message": (
                'CREATE TABLE "message" ("id" TEXT PRIMARY KEY NOT NULL, '
                '"conversation_id" TEXT NOT NULL, "seq" INTEGER NOT NULL, '
                '"role" TEXT NOT NULL, "content" TEXT NOT NULL, '
                '"created_at" TEXT NOT NULL, "pi_message_id" TEXT, '
                '"tool_args" TEXT, "tool_name" TEXT, "tool_result" TEXT) STRICT'
            ),
            "test_seed_conversation": (
                'INSERT INTO "conversation" '
                '("id", "created_at", "pi_session_id", "selected_model") '
                f"VALUES ('{_OLDEST_ID}', '2025-01-01T00:00:00.000Z', "
                "'018f0000-0000-7000-8000-000000000011', 'smart')"
            ),
            "test_seed_messages": (
                'INSERT INTO "message" '
                '("id", "conversation_id", "seq", "role", "content", '
                '"created_at") VALUES '
                f"('018f0000-0000-7000-8000-000000000101', '{_OLDEST_ID}', 1, "
                "'assistant', 'leading', '2025-01-01T00:00:01.000Z'), "
                f"('018f0000-0000-7000-8000-000000000102', '{_OLDEST_ID}', 2, "
                "'user', 'first', '2025-01-01T00:00:02.000Z'), "
                f"('018f0000-0000-7000-8000-000000000103', '{_OLDEST_ID}', 3, "
                "'reasoning', 'work', '2025-01-01T00:00:03.000Z'), "
                f"('018f0000-0000-7000-8000-000000000104', '{_OLDEST_ID}', 4, "
                "'assistant', 'answer', '2025-01-01T00:00:04.000Z'), "
                f"('018f0000-0000-7000-8000-000000000105', '{_OLDEST_ID}', 5, "
                "'user', 'unfinished', '2025-01-01T00:00:05.000Z'), "
                f"('018f0000-0000-7000-8000-000000000106', '{_OLDEST_ID}', 6, "
                "'tool', 'partial', '2025-01-01T00:00:06.000Z')"
            ),
        }
    )

    await create_conversation_schema(database)

    async with database.transaction() as transaction:
        turns = await transaction.fetch_all(
            select(ConversationTurn).all().order_by(ConversationTurn.created_at.asc())
        )
        messages = await transaction.fetch_all(
            select(Message).all().order_by(Message.seq.asc())
        )
    await database.close()

    assert_eq([turn.status for turn in turns], ["succeeded", "succeeded", "failed"])
    assert_eq([turn.origin for turn in turns], ["historical"] * 3)
    assert_eq(turns[2].failure_code, "historical_incomplete")
    assert_eq(
        [message.role for message in messages],
        [
            "assistant",
            "user",
            "reasoning",
            "assistant",
            "user",
            "tool",
        ],
    )
    assert_eq(
        [(message.turn_id, message.turn_message_seq) for message in messages],
        [
            (turns[0].id, 1),
            (turns[1].id, 1),
            (turns[1].id, 2),
            (turns[1].id, 3),
            (turns[2].id, 1),
            (turns[2].id, 2),
        ],
    )
