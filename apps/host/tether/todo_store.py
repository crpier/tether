"""SQLite models and historical schema chain for the Todo vertical."""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid7

from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)

from tether.todo_model import TodoStatus


class Todo[S = Pending](Model[S, "Todo[Fetched]"]):
    """One actionable item with an optional waiting condition and trigger link."""

    id: Todo.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    action: Todo.Col[str] = Text()
    """The single action to take, phrased in the user's terms."""
    status: Todo.Col[TodoStatus] = Text()
    """Lifecycle state: `active`, or the terminal `completed`/`abandoned`."""
    condition: Todo.Col[str | None] = Text(default=None, nullable=True)
    """Free-text waiting condition ("next time I visit Ana"); null when none."""
    trigger_id: Todo.Col[str | None] = Text(default=None, nullable=True)
    """The linked scheduled once-trigger (a deadline), if any; a plain nullable
    reference, not a DB-enforced foreign key (mirrors `Notification.trigger_id`)."""
    version: Todo.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: Todo.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: Todo.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(status)]


async def create_todo_schema(database: Database) -> None:
    """Create the Todo and Todo-Memory tables and their indexes.

    Applied as its own ordered migrations after the earlier schemas. Scaffolding
    emits one statement per table/index, and a snekql migration body runs exactly
    one statement, so each becomes its own ordered migration.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_todo_schema(database)
    """
    migrations = {
        "013_create_todo": (
            'CREATE TABLE "todo" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "action" TEXT NOT NULL, '
            '"status" TEXT NOT NULL, "condition" TEXT, "trigger_id" TEXT, '
            '"version" INTEGER NOT NULL, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"updated_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "013_create_index_ix_todo_status": (
            'CREATE INDEX "ix_todo_status" ON "todo" ("status")'
        ),
        # Frozen historical link table, immediately removed by the #507
        # destructive cutover below on fresh and upgraded databases alike.
        "013_create_todo_memory": (
            'CREATE TABLE "todo_memory" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "todo_id" TEXT NOT NULL, '
            '"memory_id" TEXT NOT NULL, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "013_create_index_ix_todo_memory_todo_id": (
            'CREATE INDEX "ix_todo_memory_todo_id" ON "todo_memory" ("todo_id")'
        ),
        "026_drop_todo_memory": 'DROP TABLE "todo_memory"',
    }
    await database.migrate(migrations)
