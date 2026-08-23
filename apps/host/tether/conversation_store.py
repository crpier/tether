"""Canonical SQLite models and schema for host-owned conversations."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid7

from pydantic import UUID7, NonNegativeInt, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)

from tether.conversation_model import (
    ConversationKind,
    ConversationStatus,
    ConversationTurnOrigin,
    ConversationTurnStatus,
    MessageRole,
)


class Conversation[S = Pending](Model[S, "Conversation[Fetched]"]):
    """A stable host-owned chat thread."""

    id: Conversation.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    archived_at: Conversation.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    created_at: Conversation.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    display_name: Conversation.Col[str | None] = Text(default=None, nullable=True)
    kind: Conversation.Col[ConversationKind] = Text(
        default=cast("ConversationKind", "scoped")
    )
    last_read_seq: Conversation.Col[NonNegativeInt] = Integer(default=0)
    pi_session_id: Conversation.GenCol[UUID7] = Text(default_factory=uuid7)
    scope_brief: Conversation.Col[str | None] = Text(default=None, nullable=True)
    scope_revision: Conversation.Col[PositiveInt] = Integer(default=1)
    runtime_scope_revision: Conversation.Col[PositiveInt] = Integer(default=1)
    selected_model: Conversation.Col[str | None] = Text(default=None, nullable=True)
    status: Conversation.Col[ConversationStatus] = Text(
        default=cast("ConversationStatus", "active")
    )
    title: Conversation.Col[str | None] = Text(default=None, nullable=True)


class ConversationTurn[S = Pending](Model[S, "ConversationTurn[Fetched]"]):
    """One durable FIFO execution owned by a Conversation."""

    id: ConversationTurn.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    acceptance_started_at: ConversationTurn.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    attempts: ConversationTurn.Col[NonNegativeInt] = Integer(default=0)
    cancel_requested_at: ConversationTurn.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    completed_at: ConversationTurn.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    conversation_id: ConversationTurn.Col[UUID7] = Text()
    created_at: ConversationTurn.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    failure_code: ConversationTurn.Col[str | None] = Text(default=None, nullable=True)
    failure_phase: ConversationTurn.Col[str | None] = Text(default=None, nullable=True)
    execution_lease_id: ConversationTurn.Col[UUID | None] = Text(
        default=None,
        nullable=True,
    )
    failure_summary: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_display_name_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_id_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_provider_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_thinking_level_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    origin: ConversationTurn.Col[ConversationTurnOrigin] = Text()
    prompt_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    reply_mode: ConversationTurn.Col[str] = Text(default="text")
    request_id: ConversationTurn.Col[UUID | None] = Text(
        default=None,
        nullable=True,
    )
    scope_brief_snapshot: ConversationTurn.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    scope_revision_snapshot: ConversationTurn.Col[PositiveInt] = Integer(default=1)
    scheduled_occurrence_id: ConversationTurn.Col[UUID | None] = Text(
        default=None,
        nullable=True,
    )
    started_at: ConversationTurn.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    status: ConversationTurn.Col[ConversationTurnStatus] = Text()
    trace_run_id: ConversationTurn.Col[str | None] = Text(default=None, nullable=True)
    turn_seq: ConversationTurn.Col[PositiveInt] = Integer()


class Message[S = Pending](Model[S, "Message[Fetched]"]):
    """One settled transcript row owned by the host."""

    id: Message.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    conversation_id: Message.Col[UUID7] = Text()
    seq: Message.Col[PositiveInt] = Integer()
    role: Message.Col[MessageRole] = Text()
    content: Message.Col[str] = Text()
    created_at: Message.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    pi_message_id: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_args: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_name: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_result: Message.Col[str | None] = Text(default=None, nullable=True)
    turn_id: Message.Col[UUID7 | None] = Text(default=None, nullable=True)
    turn_message_seq: Message.Col[PositiveInt | None] = Integer(
        default=None,
        nullable=True,
    )


async def _backfill_historical_turns(database: Database) -> None:
    """Group each legacy transcript without inferring Scheduled provenance."""
    async with database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        cursor = await connection.execute(
            """SELECT "id", "selected_model", "scope_brief", "scope_revision"
            FROM "conversation" ORDER BY "created_at", "id"
            """,
            (),
        )
        conversations = cast("list[tuple[Any, ...]]", await cursor.fetchall())
        await cursor.close()
        for conversation in conversations:
            conversation_id = UUID(str(conversation[0]))
            cursor = await connection.execute(
                """SELECT "id", "seq", "role", "content", "created_at"
                FROM "message" WHERE "conversation_id" = ?
                AND "turn_id" IS NULL ORDER BY "seq"
                """,
                (str(conversation_id),),
            )
            rows = cast("list[tuple[Any, ...]]", await cursor.fetchall())
            await cursor.close()
            groups: list[list[tuple[Any, ...]]] = []
            for row in rows:
                if str(row[2]) == "user" or not groups:
                    groups.append([])
                groups[-1].append(row)
            existing_turn_count_cursor = await connection.execute(
                'SELECT COUNT(*) FROM "conversation_turn" WHERE "conversation_id" = ?',
                (str(conversation_id),),
            )
            existing_turn_count_row = cast(
                "tuple[Any, ...] | None",
                await existing_turn_count_cursor.fetchone(),
            )
            await existing_turn_count_cursor.close()
            existing_turn_count = (
                0
                if existing_turn_count_row is None
                else int(cast("int", existing_turn_count_row[0]))
            )
            for turn_seq, group in enumerate(groups, start=existing_turn_count + 1):
                turn_id = uuid7()
                has_assistant = any(str(row[2]) == "assistant" for row in group)
                initiating_user = next(
                    (row for row in group if str(row[2]) == "user"),
                    None,
                )
                insert_cursor = await connection.execute(
                    """INSERT INTO "conversation_turn" (
                    "id", "attempts", "completed_at", "conversation_id",
                    "created_at", "failure_code", "failure_phase",
                    "failure_summary", "model_snapshot", "origin",
                    "prompt_snapshot", "reply_mode", "scope_brief_snapshot",
                    "scope_revision_snapshot", "started_at", "status", "turn_seq")
                    VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(turn_id),
                        str(group[-1][4]),
                        str(conversation_id),
                        str(group[0][4]),
                        None if has_assistant else "historical_incomplete",
                        None if has_assistant else "historical",
                        None
                        if has_assistant
                        else "Historical turn did not contain an assistant message.",
                        conversation[1],
                        "historical",
                        None if initiating_user is None else str(initiating_user[3]),
                        "text",
                        conversation[2],
                        int(conversation[3]),
                        str(group[0][4]),
                        "succeeded" if has_assistant else "failed",
                        turn_seq,
                    ),
                )
                await insert_cursor.close()
                for turn_message_seq, row in enumerate(group, start=1):
                    update_cursor = await connection.execute(
                        """UPDATE "message" SET "turn_id" = ?,
                        "turn_message_seq" = ? WHERE "id" = ?
                        """,
                        (str(turn_id), turn_message_seq, str(row[0])),
                    )
                    await update_cursor.close()


async def create_conversation_schema(database: Database) -> None:
    """Create or migrate canonical Conversation and Message persistence."""
    migrations = {
        "003_create_conversation": (
            'CREATE TABLE "conversation" ("id" TEXT PRIMARY KEY NOT NULL, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"pi_session_id" TEXT NOT NULL, "selected_model" TEXT, '
            '"title" TEXT) STRICT'
        ),
        "003_create_message": (
            'CREATE TABLE "message" ("id" TEXT PRIMARY KEY NOT NULL, '
            '"conversation_id" TEXT NOT NULL, "seq" INTEGER NOT NULL, '
            '"role" TEXT NOT NULL, "content" TEXT NOT NULL, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"pi_message_id" TEXT, "tool_args" TEXT, "tool_name" TEXT, '
            '"tool_result" TEXT) STRICT'
        ),
        "034_conversation_archived_at": (
            'ALTER TABLE "conversation" ADD COLUMN "archived_at" TEXT'
        ),
        "034_conversation_display_name": (
            'ALTER TABLE "conversation" ADD COLUMN "display_name" TEXT'
        ),
        "034_conversation_kind": (
            'ALTER TABLE "conversation" ADD COLUMN "kind" TEXT NOT NULL '
            "DEFAULT 'scoped'"
        ),
        "034_conversation_last_read_seq": (
            'ALTER TABLE "conversation" ADD COLUMN "last_read_seq" INTEGER '
            "NOT NULL DEFAULT 0"
        ),
        "034_conversation_scope_brief": (
            'ALTER TABLE "conversation" ADD COLUMN "scope_brief" TEXT'
        ),
        "034_conversation_scope_revision": (
            'ALTER TABLE "conversation" ADD COLUMN "scope_revision" INTEGER '
            "NOT NULL DEFAULT 1"
        ),
        "034_conversation_status": (
            'ALTER TABLE "conversation" ADD COLUMN "status" TEXT NOT NULL '
            "DEFAULT 'active'"
        ),
        "034_conversation_migrate_existing": (
            'UPDATE "conversation" SET "status" = \'archived\', '
            '"archived_at" = "created_at", '
            '"display_name" = COALESCE(NULLIF(TRIM("title"), \'\'), '
            "'Imported conversation ' || SUBSTR(\"id\", 1, 8)), "
            "\"scope_brief\" = 'No additional scope.'"
        ),
        "034_conversation_mark_oldest_main": (
            'UPDATE "conversation" SET "kind" = \'main\', '
            '"status" = \'active\', "archived_at" = NULL, '
            '"display_name" = NULL, "scope_brief" = NULL, "title" = NULL '
            'WHERE "id" = (SELECT "id" FROM "conversation" '
            'ORDER BY "created_at" ASC, "id" ASC LIMIT 1)'
        ),
        "034_conversation_create_current": (
            'CREATE TABLE "conversation_current" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "archived_at" TEXT, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"display_name" TEXT, "kind" TEXT NOT NULL, '
            '"last_read_seq" INTEGER NOT NULL, "pi_session_id" TEXT NOT NULL, '
            '"scope_brief" TEXT, "scope_revision" INTEGER NOT NULL, '
            '"selected_model" TEXT, "status" TEXT NOT NULL, "title" TEXT) STRICT'
        ),
        "034_conversation_copy_current": (
            'INSERT INTO "conversation_current" ('
            '"id", "archived_at", "created_at", "display_name", "kind", '
            '"last_read_seq", "pi_session_id", "scope_brief", "scope_revision", '
            '"selected_model", "status", "title") SELECT '
            '"id", "archived_at", "created_at", "display_name", "kind", '
            '"last_read_seq", "pi_session_id", "scope_brief", "scope_revision", '
            '"selected_model", "status", "title" FROM "conversation"'
        ),
        "034_conversation_drop_legacy": 'DROP TABLE "conversation"',
        "034_conversation_rename_current": (
            'ALTER TABLE "conversation_current" RENAME TO "conversation"'
        ),
        "034_conversation_single_main_insert": (
            'CREATE TRIGGER "conversation_single_main_insert" BEFORE INSERT '
            'ON "conversation" WHEN NEW."kind" = \'main\' AND EXISTS ('
            'SELECT 1 FROM "conversation" WHERE "kind" = \'main\') '
            "BEGIN SELECT RAISE(ABORT, 'Main Conversation already exists'); END"
        ),
        "034_conversation_single_main_update": (
            'CREATE TRIGGER "conversation_single_main_update" BEFORE UPDATE '
            'OF "kind" ON "conversation" WHEN NEW."kind" = \'main\' AND EXISTS ('
            'SELECT 1 FROM "conversation" WHERE "kind" = \'main\' '
            'AND "id" != NEW."id") '
            "BEGIN SELECT RAISE(ABORT, 'Main Conversation already exists'); END"
        ),
        "035_conversation_runtime_scope_revision": (
            'ALTER TABLE "conversation" ADD COLUMN "runtime_scope_revision" '
            "INTEGER NOT NULL DEFAULT 1"
        ),
        "035_create_conversation_turn": (
            'CREATE TABLE "conversation_turn" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "acceptance_started_at" TEXT, '
            '"attempts" INTEGER NOT NULL DEFAULT 0, "completed_at" TEXT, '
            '"conversation_id" TEXT NOT NULL, "created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"failure_code" TEXT, "failure_phase" TEXT, "failure_summary" TEXT, '
            '"model_snapshot" TEXT, "origin" TEXT NOT NULL, '
            '"prompt_snapshot" TEXT, "reply_mode" TEXT NOT NULL DEFAULT \'text\', '
            '"request_id" TEXT, "scope_brief_snapshot" TEXT, '
            '"scope_revision_snapshot" INTEGER NOT NULL DEFAULT 1, '
            '"scheduled_occurrence_id" TEXT, "started_at" TEXT, '
            '"status" TEXT NOT NULL, "trace_run_id" TEXT) STRICT'
        ),
        "035_message_turn_id": ('ALTER TABLE "message" ADD COLUMN "turn_id" TEXT'),
        "035_message_turn_message_seq": (
            'ALTER TABLE "message" ADD COLUMN "turn_message_seq" INTEGER'
        ),
        "035_turn_interactive_request_unique": (
            'CREATE UNIQUE INDEX "conversation_turn_interactive_request_unique" '
            'ON "conversation_turn" ("request_id") WHERE "request_id" IS NOT NULL'
        ),
        "035_turn_scheduled_occurrence_unique": (
            'CREATE UNIQUE INDEX "conversation_turn_scheduled_occurrence_unique" '
            'ON "conversation_turn" ("scheduled_occurrence_id") '
            'WHERE "scheduled_occurrence_id" IS NOT NULL'
        ),
        "035_turn_message_sequence_unique": (
            'CREATE UNIQUE INDEX "message_turn_sequence_unique" '
            'ON "message" ("turn_id", "turn_message_seq") '
            'WHERE "turn_id" IS NOT NULL'
        ),
        "037_turn_cancel_requested_at": (
            'ALTER TABLE "conversation_turn" ADD COLUMN "cancel_requested_at" TEXT'
        ),
        "037_turn_execution_lease_id": (
            'ALTER TABLE "conversation_turn" ADD COLUMN "execution_lease_id" TEXT'
        ),
        "037_turn_model_display_name_snapshot": (
            'ALTER TABLE "conversation_turn" ADD COLUMN '
            '"model_display_name_snapshot" TEXT'
        ),
        "037_turn_model_id_snapshot": (
            'ALTER TABLE "conversation_turn" ADD COLUMN "model_id_snapshot" TEXT'
        ),
        "037_turn_model_provider_snapshot": (
            'ALTER TABLE "conversation_turn" ADD COLUMN "model_provider_snapshot" TEXT'
        ),
        "037_turn_model_thinking_level_snapshot": (
            'ALTER TABLE "conversation_turn" ADD COLUMN '
            '"model_thinking_level_snapshot" TEXT'
        ),
        "037_turn_seq": (
            'ALTER TABLE "conversation_turn" ADD COLUMN "turn_seq" INTEGER '
            "NOT NULL DEFAULT 1"
        ),
        "037_turn_seq_backfill": (
            'UPDATE "conversation_turn" AS target SET "turn_seq" = ('
            'SELECT COUNT(*) FROM "conversation_turn" AS preceding WHERE '
            'preceding."conversation_id" = target."conversation_id" AND ('
            'preceding."created_at" < target."created_at" OR ('
            'preceding."created_at" = target."created_at" AND '
            'preceding."id" <= target."id")))'
        ),
        "037_turn_sequence_unique": (
            'CREATE UNIQUE INDEX "conversation_turn_sequence_unique" '
            'ON "conversation_turn" ("conversation_id", "turn_seq")'
        ),
        "037_turn_initiating_message_unique": (
            'CREATE UNIQUE INDEX "message_turn_initiating_unique" '
            'ON "message" ("turn_id") WHERE "turn_id" IS NOT NULL AND '
            "\"role\" IN ('user', 'scheduled')"
        ),
    }
    await database.migrate(migrations)
    await _backfill_historical_turns(database)


__all__ = [
    "Conversation",
    "ConversationTurn",
    "Message",
    "create_conversation_schema",
]
