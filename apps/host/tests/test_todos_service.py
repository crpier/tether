"""Behavior tests for canonical Todo operations."""

from collections.abc import AsyncGenerator

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database, update
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.structured_logging import Logger
from tether.todo_errors import InvalidTodoError, TodoConflictError
from tether.todo_store import Todo, create_todo_schema
from tether.todos import TodoService, todo_reference

LOGGER: Logger = structlog.stdlib.get_logger("test.todos")


@fixture
async def todos() -> AsyncGenerator[TodoService]:
    """Create the retained Todo service over canonical SQLite."""
    database = await Database.initialize(Config(database=":memory:"))
    await create_todo_schema(database)
    yield TodoService(
        database=database,
        tracer=trace.NoOpTracerProvider().get_tracer("test.todos"),
    )
    await database.close()


@test()
async def create_normalizes_action_and_condition() -> None:
    """Todo text is trimmed before canonical storage."""
    service = await load_fixture(todos())

    todo = await service.create(
        "  call the dentist  ", condition="  after work  ", logger=LOGGER
    )

    assert_eq(todo.action, "call the dentist")
    assert_eq(todo.condition, "after work")


@test()
async def create_rejects_a_blank_action() -> None:
    """A Todo always names one concrete action."""
    service = await load_fixture(todos())

    with assert_raises(InvalidTodoError):
        _ = await service.create("  ", logger=LOGGER)


@test()
async def readiness_ignores_inert_legacy_trigger_links() -> None:
    """Only a free-text condition places an active Todo in waiting."""
    service = await load_fixture(todos())
    ready = await service.create("ready", logger=LOGGER)
    _ = await service.create("waiting", condition="after work", logger=LOGGER)
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            update(Todo)
            .set(Todo.trigger_id.to("legacy-trigger"))
            .where(Todo.id.eq(ready.id))
        )

    readiness = await service.readiness(logger=LOGGER)

    assert_eq([todo.action for todo in readiness.ready], ["ready"])
    assert_eq([todo.action for todo in readiness.waiting], ["waiting"])


@test()
async def readiness_caps_ready_results() -> None:
    """One tool read cannot return more than the ready collection cap."""
    service = await load_fixture(todos())
    for index in range(11):
        _ = await service.create(f"ready {index}", logger=LOGGER)

    readiness = await service.readiness(logger=LOGGER)

    assert_eq(len(readiness.ready), 10)


@test()
async def readiness_caps_waiting_results() -> None:
    """One tool read cannot return more than the waiting collection cap."""
    service = await load_fixture(todos())
    for index in range(16):
        _ = await service.create(
            f"waiting {index}", condition="after work", logger=LOGGER
        )

    readiness = await service.readiness(logger=LOGGER)

    assert_eq(len(readiness.waiting), 15)


@test()
async def stale_status_updates_conflict() -> None:
    """A stale observed Todo version cannot overwrite a newer status."""
    service = await load_fixture(todos())
    todo = await service.create("call the dentist", logger=LOGGER)
    _ = await service.set_status(todo, "completed", logger=LOGGER)

    with assert_raises(TodoConflictError):
        _ = await service.set_status(
            todo_reference(todo.id, todo.version), "active", logger=LOGGER
        )
