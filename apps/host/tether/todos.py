"""The Todo vertical: a single-action item with a computed waiting state.

A Todo is one thing to do — "bring the book next time I visit Ana", "dig out the
grey shirt before the gala", "research the pension transfer". It is distinct from
a Bucket item (an intention to *consume* something) and a Project (a multi-step
undertaking): a Todo is exactly one action, born active, and reaching a terminal
`completed` or `abandoned` state through the base-set lifecycle convention (the
status column is a plain string, not an enum enforced in the schema — ADR 0016's
bespoke idiom).

A Todo can carry an optional **waiting condition**, in two coexisting forms: a
free-text `condition` ("next time I visit Ana") for event-triggered tasks that
resist a date, and/or a link to a scheduled once-`trigger` (a deadline) that
fires mechanically. Neither is required; a Todo with neither is simply ready now.

"Waiting" is **computed, never stored**, so a Todo can never get wedged in a
stale waiting state: a Todo is *waiting* while it has an unmet text condition or
an unfired linked trigger, and *ready* otherwise. Trigger firing is read off the
notification history (the same precedent ADR 0017 §d cites for the Project
vertical), so readiness introduces no new write path — a fired trigger has left a
`Notification` row carrying its `trigger_id`.

>>> service = TodoService(database=database, tracer=tracer)
>>> todo = await service.create("call the dentist", logger=logger)
>>> todo.status
'active'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import cast
from uuid import UUID

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.notification_store import Notification
from tether.structured_logging import Logger
from tether.todo_errors import InvalidTodoError, TodoConflictError, TodoNotFoundError
from tether.todo_model import READY_DIGEST_CAP, WAITING_DIGEST_CAP, TodoStatus
from tether.todo_store import Todo
from tether.trigger_store import ScheduledTrigger


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


def _normalise_action(action: str) -> str:
    """Trim a Todo's action text, rejecting a blank one."""
    normalised = action.strip()
    if not normalised:
        message = "todo action must not be blank"
        raise InvalidTodoError(message)
    return normalised


def _normalise_condition(condition: str | None) -> str | None:
    """Trim a waiting condition; a blank or omitted one stores as `None`."""
    if condition is None:
        return None
    normalised = condition.strip()
    return normalised or None


@dataclass(frozen=True, slots=True)
class TodoReadiness:
    """The live active Todos split into ready and waiting, with their deadlines.

    `ready` are the Todos actionable now (no unmet condition, no unfired
    trigger), newest first. `waiting` carry an unmet text condition or an unfired
    trigger, soonest deadline first then newest. `deadlines` maps a waiting
    Todo's id to its unfired trigger's next fire time, when it has one.
    """

    ready: list[Todo[Fetched]]
    waiting: list[Todo[Fetched]]
    deadlines: dict[UUID7, datetime] = field(default_factory=dict[UUID7, datetime])


def todo_reference(todo_id: UUID, version: PositiveInt) -> Todo[Fetched]:
    """Build a detached Todo carrying only the identity a mutation acts on.

    Status transitions and links read just `id` and `version` to run their
    optimistic-concurrency check and re-fetch the live row, so a hand-built
    reference is enough; the other columns are required placeholders.
    """
    return cast(
        "Todo[Fetched]",
        Todo.construct(id=todo_id, version=version, action="", status="active"),
    )


class TodoService:
    """Capability surface for Todos, over a snekql database.

    Each mutation owns its own transaction (one mutation, one commit) and returns
    the resulting Todo so the REST and tool layers can echo it. Readiness is
    computed on read from the trigger and notification history — never stored.
    """

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.tracer: Tracer = tracer

    async def create(
        self,
        action: str,
        *,
        condition: str | None = None,
        trigger_id: str | None = None,
        logger: Logger,
    ) -> Todo[Fetched]:
        """Create an active Todo, optionally with a waiting condition/trigger."""
        normalised_action = _normalise_action(action)
        normalised_condition = _normalise_condition(condition)
        with self.tracer.start_as_current_span("TodoService.create") as span:
            _debug(
                logger, "Creating Todo", has_condition=normalised_condition is not None
            )

            async def _create(tx: Transaction) -> Todo[Fetched]:
                return await tx.execute(
                    insert(
                        Todo(
                            action=normalised_action,
                            status="active",
                            condition=normalised_condition,
                            trigger_id=trigger_id,
                        )
                    ).returning()
                )

            async with self.database.transaction(mode="immediate") as tx:
                todo = await _create(tx)
            span.set_attribute("todo.id", str(todo.id))
            _info(logger, "Todo created", todo_id=str(todo.id))
        await self.event_publisher.publish(InvalidateEvent(keys=["todos"]))
        return todo

    async def set_status(
        self,
        todo: Todo[Fetched],
        status: TodoStatus,
        *,
        logger: Logger,
    ) -> Todo[Fetched]:
        """Transition a Todo to a new status at an observed version.

        A stale observed version conflicts; an absent Todo raises. Any status is
        reachable — the graduation hand-off sets a Todo `abandoned`, and a
        mistaken completion can be walked back to `active`.
        """
        _debug(
            logger,
            "Setting Todo status",
            todo_id=str(todo.id),
            status=status,
            observed_version=todo.version,
        )

        async def _set_status(tx: Transaction) -> Todo[Fetched]:
            matched = await tx.execute(
                update(Todo)
                .set(Todo.status.to(status))
                .set(Todo.updated_at.to(CurrentTimestamp))
                .set(Todo.version.to(todo.version + 1))
                .where(Todo.id.eq(todo.id))
                .where(Todo.version.eq(todo.version))
            )
            fresh = await self._fetch(tx, todo.id)
            if matched == 0:
                self._raise_version_conflict(todo, fresh)
            return fresh

        async with self.database.transaction(mode="immediate") as tx:
            fresh = await _set_status(tx)
        _info(
            logger,
            "Todo status set",
            todo_id=str(fresh.id),
            status=fresh.status,
            version=fresh.version,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["todos"]))
        return fresh

    async def link_trigger(
        self,
        todo: Todo[Fetched],
        trigger_id: str,
        *,
        logger: Logger,
    ) -> Todo[Fetched]:
        """Attach a scheduled trigger (a deadline) to a Todo at its version."""
        _debug(
            logger,
            "Linking Todo trigger",
            todo_id=str(todo.id),
            observed_version=todo.version,
        )

        async def _link(tx: Transaction) -> Todo[Fetched]:
            matched = await tx.execute(
                update(Todo)
                .set(Todo.trigger_id.to(trigger_id))
                .set(Todo.updated_at.to(CurrentTimestamp))
                .set(Todo.version.to(todo.version + 1))
                .where(Todo.id.eq(todo.id))
                .where(Todo.version.eq(todo.version))
            )
            fresh = await self._fetch(tx, todo.id)
            if matched == 0:
                self._raise_version_conflict(todo, fresh)
            return fresh

        async with self.database.transaction(mode="immediate") as tx:
            fresh = await _link(tx)
        _info(logger, "Todo trigger linked", todo_id=str(fresh.id))
        await self.event_publisher.publish(InvalidateEvent(keys=["todos"]))
        return fresh

    async def list_by_status(
        self, status: TodoStatus, *, logger: Logger
    ) -> list[Todo[Fetched]]:
        """List Todos in a lifecycle state, newest first."""
        _debug(logger, "Listing Todos by status", status=status)
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(Todo)
                .where(Todo.status.eq(status))
                .order_by(Todo.created_at.desc())
            )

    async def readiness(self, *, now: datetime, logger: Logger) -> TodoReadiness:
        """Split the active Todos into ready and waiting, computing each.

        Waiting is derived, not stored: a Todo waits while it has an unmet text
        condition or an unfired linked trigger. A trigger has fired when the
        notification history carries a row for its id — no new write path. Ready
        Todos come back newest first, capped for the digest; waiting Todos come
        back soonest-deadline first, then newest.
        """
        _debug(logger, "Computing Todo readiness")
        async with self.database.transaction() as tx:
            active = await tx.fetch_all(
                select(Todo)
                .where(Todo.status.eq("active"))
                .order_by(Todo.created_at.desc())
            )
            trigger_ids = [todo.trigger_id for todo in active if todo.trigger_id]
            fired: set[str] = set()
            fire_times: dict[str, datetime] = {}
            if trigger_ids:
                notifications = await tx.fetch_all(
                    select(Notification).where(
                        Notification.trigger_id.in_(*trigger_ids)
                    )
                )
                fired = {
                    n.trigger_id for n in notifications if n.trigger_id is not None
                }
                triggers = await tx.fetch_all(
                    select(ScheduledTrigger).where(
                        ScheduledTrigger.id.in_(*[UUID(tid) for tid in trigger_ids])
                    )
                )
                fire_times = {str(t.id): t.next_fire_at for t in triggers}

        ready: list[Todo[Fetched]] = []
        waiting: list[Todo[Fetched]] = []
        deadlines: dict[UUID7, datetime] = {}
        for todo in active:
            has_condition = todo.condition is not None
            unfired_trigger = (
                todo.trigger_id is not None and todo.trigger_id not in fired
            )
            if unfired_trigger and todo.trigger_id in fire_times:
                deadlines[todo.id] = fire_times[todo.trigger_id]
            if has_condition or unfired_trigger:
                waiting.append(todo)
            else:
                ready.append(todo)

        waiting.sort(
            key=lambda todo: (
                deadlines.get(todo.id) is None,
                deadlines.get(todo.id) or now,
            )
        )
        return TodoReadiness(
            ready=ready[:READY_DIGEST_CAP],
            waiting=waiting[:WAITING_DIGEST_CAP],
            deadlines=deadlines,
        )

    async def _fetch(self, tx: Transaction, todo_id: UUID) -> Todo[Fetched]:
        """Fetch a Todo by id in any state, or raise when genuinely absent."""
        todo = await tx.fetch_one_or_none(select(Todo).where(Todo.id.eq(todo_id)))
        if todo is None:
            raise TodoNotFoundError(todo_id)
        return todo

    def _raise_version_conflict(
        self, observed: Todo[Fetched], current: Todo[Fetched]
    ) -> None:
        """Raise the optimistic-concurrency conflict for a stale write."""
        msg = (
            f"Tried to update Todo {observed.id} with version "
            f"{observed.version} but it had version {current.version}"
        )
        raise TodoConflictError(msg)
