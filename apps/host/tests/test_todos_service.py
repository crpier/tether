"""Behaviour tests for the Todo service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent — the primary testing seam: call a capability and
assert on observable behaviour (DB rows, the computed readiness split), never on
internal structure.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_false,
    assert_is_none,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.notifications import (
    NotificationDraft,
    NotificationService,
    create_notification_schema,
)
from tether.todos import (
    InvalidTodoError,
    TodoConflictError,
    TodoNotFoundError,
    TodoService,
    create_todo_schema,
    todo_reference,
)
from tether.triggers import TriggerService, TriggerSpec, create_trigger_schema


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.todos")


def test_logger() -> Logger:
    """A throwaway structlog logger for the service's mandatory logging arg."""
    return structlog.get_logger("test.todos")


class TodoEnv:
    """A Todo-ready database plus its collaborating services."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database
        self.logger: Logger = test_logger()
        self.service: TodoService = TodoService(database=database, tracer=noop_tracer())
        self.triggers: TriggerService = TriggerService(
            database=database, tracer=noop_tracer()
        )
        self.notifications: NotificationService = NotificationService(database=database)


@fixture
async def todo_env() -> AsyncGenerator[TodoEnv]:
    """A fresh database carrying the Todo, Trigger, and Notification schemas."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_trigger_schema(db)
    await create_notification_schema(db)
    await create_todo_schema(db)
    yield TodoEnv(db)
    await db.close()


@test()
async def create_stores_an_active_todo() -> None:
    """A created Todo is active with its action trimmed."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("  call the dentist  ", logger=env.logger)
    assert_eq(todo.action, "call the dentist")
    assert_eq(todo.status, "active")
    assert_is_none(todo.condition)
    assert_is_none(todo.trigger_id)


@test()
async def create_rejects_a_blank_action() -> None:
    """A blank action after trimming is a domain error, not a corrupt row."""
    env = await load_fixture(todo_env())
    with assert_raises(InvalidTodoError):
        _ = await env.service.create("   ", logger=env.logger)


@test()
async def create_stores_a_free_text_condition() -> None:
    """A waiting condition rides along on create; a blank one stores as null."""
    env = await load_fixture(todo_env())
    with_condition = await env.service.create(
        "bring the book", condition="next time I visit Ana", logger=env.logger
    )
    assert_eq(with_condition.condition, "next time I visit Ana")
    blank = await env.service.create("x", condition="  ", logger=env.logger)
    assert_is_none(blank.condition)


@test()
async def set_status_transitions_and_bumps_version() -> None:
    """A status transition moves the Todo and bumps its version."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("water plants", logger=env.logger)
    completed = await env.service.set_status(todo, "completed", logger=env.logger)
    assert_eq(completed.status, "completed")
    assert_eq(completed.version, todo.version + 1)


@test()
async def set_status_conflicts_on_a_stale_version() -> None:
    """A stale observed version is an optimistic-concurrency conflict."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("water plants", logger=env.logger)
    _ = await env.service.set_status(todo, "completed", logger=env.logger)
    with assert_raises(TodoConflictError):
        _ = await env.service.set_status(todo, "abandoned", logger=env.logger)


@test()
async def set_status_raises_for_an_absent_todo() -> None:
    """Transitioning a Todo that does not exist raises not-found."""
    env = await load_fixture(todo_env())
    with assert_raises(TodoNotFoundError):
        _ = await env.service.set_status(
            todo_reference(uuid7(), 1), "completed", logger=env.logger
        )


@test()
async def link_memory_is_idempotent_and_guards_absence() -> None:
    """Linking the same Memory twice is a no-op; a dangling Todo raises."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("follow up", logger=env.logger)
    memory_id = uuid7()
    await env.service.link_memory(todo.id, memory_id, logger=env.logger)
    await env.service.link_memory(todo.id, memory_id, logger=env.logger)
    assert_eq(await env.service.linked_memory_ids(todo.id), [str(memory_id)])
    with assert_raises(TodoNotFoundError):
        await env.service.link_memory(uuid7(), memory_id, logger=env.logger)


@test()
async def a_bare_todo_is_ready() -> None:
    """A Todo with no condition and no trigger is ready now."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("call the dentist", logger=env.logger)
    readiness = await env.service.readiness(now=datetime.now(UTC), logger=env.logger)
    ready_ids = [t.id for t in readiness.ready]
    assert_true(todo.id in ready_ids)
    assert_eq(readiness.waiting, [])


@test()
async def a_todo_with_a_condition_is_waiting() -> None:
    """A free-text condition keeps a Todo waiting (a condition is never met)."""
    env = await load_fixture(todo_env())
    todo = await env.service.create(
        "bring the book", condition="next time I visit Ana", logger=env.logger
    )
    readiness = await env.service.readiness(now=datetime.now(UTC), logger=env.logger)
    assert_eq([t.id for t in readiness.waiting], [todo.id])
    assert_eq(readiness.ready, [])


@test()
async def an_unfired_trigger_waits_and_a_fired_one_readies() -> None:
    """A linked trigger waits until the notification history shows it fired."""
    env = await load_fixture(todo_env())
    now = datetime.now(UTC)
    trigger = await env.triggers.create(
        TriggerSpec(
            recurrence="once",
            action_kind="message",
            payload="deadline",
            fire_at=now + timedelta(days=3),
        ),
        now=now,
        logger=env.logger,
    )
    todo = await env.service.create("renew passport", logger=env.logger)
    linked = await env.service.link_trigger(todo, str(trigger.id), logger=env.logger)

    waiting = await env.service.readiness(now=now, logger=env.logger)
    assert_eq([t.id for t in waiting.waiting], [linked.id])
    assert_eq(waiting.deadlines[linked.id], trigger.next_fire_at)

    # A fired trigger leaves a notification row carrying its id — now ready.
    _ = await env.notifications.record(
        NotificationDraft(body="deadline", trigger_id=str(trigger.id))
    )
    ready = await env.service.readiness(now=now, logger=env.logger)
    assert_eq([t.id for t in ready.ready], [linked.id])
    assert_eq(ready.waiting, [])


@test()
async def a_terminal_todo_is_absent_from_readiness() -> None:
    """Completed/abandoned Todos never surface in the readiness split."""
    env = await load_fixture(todo_env())
    todo = await env.service.create("done thing", logger=env.logger)
    _ = await env.service.set_status(todo, "completed", logger=env.logger)
    readiness = await env.service.readiness(now=datetime.now(UTC), logger=env.logger)
    assert_eq(readiness.ready, [])
    assert_eq(readiness.waiting, [])
    assert_false(bool(readiness.deadlines))
