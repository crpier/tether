"""Test the one-time `action: pending` facet -> Todo backfill.

Seeds an in-memory corpus with the legacy facet convention, runs the backfill,
and asserts the observable outcome: a Todo per faceted Memory, linked back to it,
with the `action` key stripped — and idempotent on a rerun.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_not_in,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    MemoryProvenance,
    MemoryService,
    create_memory_schema,
)
from tether.notifications import create_notification_schema
from tether.todos import (
    TodoService,
    create_todo_schema,
    migrate_pending_action_facets,
)
from tether.triggers import create_trigger_schema


def noop_tracer() -> trace.Tracer:
    return trace.NoOpTracerProvider().get_tracer("test.todos_migration")


class MigrationEnv:
    def __init__(self, database: Database, memory_service: MemoryService) -> None:
        self.database: Database = database
        self.logger: Logger = structlog.get_logger("test.todos_migration")
        self.memory_service: MemoryService = memory_service
        self.todo_service: TodoService = TodoService(
            database=database, tracer=noop_tracer()
        )

    async def run(self) -> int:
        return await migrate_pending_action_facets(
            self.database, self.todo_service, self.memory_service, logger=self.logger
        )


@fixture
async def migration_env() -> AsyncGenerator[MigrationEnv]:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_trigger_schema(db)
    await create_notification_schema(db)
    await create_todo_schema(db)
    async with TemporaryDirectory() as kb_root:
        yield MigrationEnv(
            db,
            MemoryService(
                database=db,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
        )
    await db.close()


@test()
async def a_pending_action_memory_becomes_a_linked_todo() -> None:
    """A faceted Memory yields a Todo linked to it, with the facet stripped."""
    env = await load_fixture(migration_env())
    memory = await env.memory_service.capture_tethered(
        "renew the parking permit\n\nfrom the council",
        provenance=MemoryProvenance(kind="gmail"),
        facets={"action": "pending", "source": "gmail"},
        logger=env.logger,
    )

    migrated = await env.run()
    assert_eq(migrated, 1)

    todos = await env.todo_service.list_by_status("active", logger=env.logger)
    assert_eq(len(todos), 1)
    assert_eq(todos[0].action, "renew the parking permit")
    assert_eq(await env.todo_service.linked_memory_ids(todos[0].id), [str(memory.id)])

    refreshed = (
        await env.memory_service.browse_by_state("tethered", logger=env.logger)
    )[0]
    assert_not_in("action", refreshed.facets)
    assert_eq(refreshed.facets.get("source"), "gmail")


@test()
async def the_backfill_is_idempotent_on_rerun() -> None:
    """A rerun creates no second Todo and migrates nothing new."""
    env = await load_fixture(migration_env())
    _ = await env.memory_service.capture_tethered(
        "call the plumber",
        provenance=MemoryProvenance(kind="gmail"),
        facets={"action": "pending"},
        logger=env.logger,
    )

    first = await env.run()
    second = await env.run()
    assert_eq(first, 1)
    assert_eq(second, 0)
    todos = await env.todo_service.list_by_status("active", logger=env.logger)
    assert_eq(len(todos), 1)


@test()
async def a_memory_without_the_facet_is_left_alone() -> None:
    """A Memory that never carried the facet is untouched."""
    env = await load_fixture(migration_env())
    _ = await env.memory_service.capture_tethered(
        "a plain fact",
        provenance=MemoryProvenance(kind="gmail"),
        facets={"source": "gmail"},
        logger=env.logger,
    )

    migrated = await env.run()
    assert_eq(migrated, 0)
    todos = await env.todo_service.list_by_status("active", logger=env.logger)
    assert_eq(todos, [])
