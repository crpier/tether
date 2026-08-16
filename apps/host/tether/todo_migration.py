"""One-time migration from pending-action Memory facets into Todos."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from snekql.sqlite import Database, select

from tether.memory_store import Memory, tethered_corpus
from tether.todo_store import TodoMemory

if TYPE_CHECKING:
    from tether.memories import MemoryService
    from tether.structured_logging import Logger
    from tether.todos import TodoService


async def migrate_pending_action_facets(
    database: Database,
    todo_service: TodoService,
    memory_service: MemoryService,
    *,
    logger: Logger,
) -> int:
    """One-time backfill: turn `action: pending` facet Memories into Todos.

    The interim convention the Gmail gate used before this vertical wrote an
    `action: pending` facet on actionable email Memories. This lifts each such
    Memory into a Todo (its action the Memory's first line), links the Todo back
    to the source Memory, and strips the now-defunct `action` key. Idempotent:
    stripping the key is what makes a rerun a no-op, and the per-Memory link is
    de-duped, so a partial run never double-creates. Returns how many Memories
    were migrated.
    """
    async with database.transaction() as transaction:
        tethered = await transaction.fetch_all(
            tethered_corpus().order_by(Memory.tethered_at.desc())
        )
    pending = [
        memory for memory in tethered if memory.facets.get("action") == "pending"
    ]
    migrated = 0
    for memory in pending:
        if not await _todos_linked_to_memory(database, memory.id):
            body = memory.content.strip()
            action = body.splitlines()[0] if body else "follow up"
            todo = await todo_service.create(action, logger=logger)
            await todo_service.link_memory(todo.id, memory.id, logger=logger)
        stripped = {
            key: value for key, value in memory.facets.items() if key != "action"
        }
        _ = await memory_service.edit_content(
            memory, memory.content, facets=stripped, logger=logger
        )
        migrated += 1
    if migrated:
        logger.info("Migrated pending-action facet Memories to Todos", count=migrated)
    return migrated


async def _todos_linked_to_memory(database: Database, memory_id: UUID) -> list[str]:
    """Todo ids already linked to a Memory, for the backfill's idempotency guard."""
    async with database.transaction() as transaction:
        links = await transaction.fetch_all(
            select(TodoMemory).where(TodoMemory.memory_id.eq(str(memory_id)))
        )
    return [link.todo_id for link in links]
