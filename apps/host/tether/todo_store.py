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
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

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


class TodoMemory[S = Pending](Model[S, "TodoMemory[Fetched]"]):
    """A bespoke link between a Todo and a Memory that carries its context."""

    id: TodoMemory.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    todo_id: TodoMemory.Col[str] = Text()
    memory_id: TodoMemory.Col[str] = Text()
    created_at: TodoMemory.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(todo_id)]


async def create_todo_schema(database: Database) -> None:
    """Create the Todo and Todo-Memory tables and their indexes.

    Applied as its own ordered migrations after the earlier schemas. Scaffolding
    emits one statement per table/index, and a snekql migration body runs exactly
    one statement, so each becomes its own ordered migration.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_todo_schema(database)
    """
    migrations = {
        f"013_{label}": sql
        for label, sql in scaffold_sqlite_statements([Todo, TodoMemory])
    }
    await database.migrate(migrations)
