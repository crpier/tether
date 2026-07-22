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

Memory links live in a bespoke `todo_memories` table (no generic edge table, per
ADR 0016), so the context that produced a Todo travels with it.

>>> service = TodoService(database=database, tracer=tracer)
>>> todo = await service.create("call the dentist", logger=logger)
>>> todo.status
'active'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal, cast
from uuid import UUID, uuid7

from opentelemetry.trace import Tracer
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
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger
from tether.notifications import Notification
from tether.triggers import ScheduledTrigger

if TYPE_CHECKING:
    from tether.memories import MemoryService

type TodoStatus = Literal["active", "completed", "abandoned"]
"""A Todo's lifecycle state. `active` is live; `completed`/`abandoned` are
terminal. A convention over the string column, not a schema-enforced enum."""

READY_DIGEST_CAP = 10
"""How many ready Todos the standing digest carries before it stops, so a
growing backlog can't bloat every conversation's system prompt."""

WAITING_DIGEST_CAP = 15
"""How many waiting Todos the digest lists for relevance-gated mention."""


class TodoNotFoundError(Exception):
    """Raised when an operation targets a Todo that does not exist."""


class TodoConflictError(Exception):
    """Raised when a live Todo cannot accept the requested operation.

    A stale observed version, not absence: the caller acted on a Todo that has
    moved on since it was read.
    """


class InvalidTodoError(Exception):
    """Raised when a Todo's action text is blank after trimming."""


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
        msg = "todo action must not be blank"
        raise InvalidTodoError(msg)
    return normalised


def _normalise_condition(condition: str | None) -> str | None:
    """Trim a waiting condition; a blank or omitted one stores as `None`."""
    if condition is None:
        return None
    normalised = condition.strip()
    return normalised or None


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
    created_at: Todo.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: Todo.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(status)]


class TodoMemory[S = Pending](Model[S, "TodoMemory[Fetched]"]):
    """A bespoke link between a Todo and a Memory that carries its context."""

    id: TodoMemory.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    todo_id: TodoMemory.Col[str] = Text()
    memory_id: TodoMemory.Col[str] = Text()
    created_at: TodoMemory.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(todo_id)]


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

            todo = await run_in_transaction(self.database, _create)
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

        fresh = await run_in_transaction(self.database, _set_status)
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

        fresh = await run_in_transaction(self.database, _link)
        _info(logger, "Todo trigger linked", todo_id=str(fresh.id))
        await self.event_publisher.publish(InvalidateEvent(keys=["todos"]))
        return fresh

    async def link_memory(
        self,
        todo_id: UUID,
        memory_id: UUID,
        *,
        logger: Logger,
    ) -> None:
        """Link a Memory to a Todo, idempotently (a repeat link is a no-op).

        Raises when the Todo does not exist so a link never dangles.
        """
        _debug(
            logger,
            "Linking Todo memory",
            todo_id=str(todo_id),
            memory_id=str(memory_id),
        )

        async def _link(tx: Transaction) -> None:
            _ = await self._fetch(tx, todo_id)
            existing = await tx.fetch_one_or_none(
                select(TodoMemory)
                .where(TodoMemory.todo_id.eq(str(todo_id)))
                .where(TodoMemory.memory_id.eq(str(memory_id)))
            )
            if existing is not None:
                return
            _ = await tx.execute(
                insert(
                    TodoMemory(todo_id=str(todo_id), memory_id=str(memory_id))
                ).returning()
            )

        await run_in_transaction(self.database, _link)
        await self.event_publisher.publish(InvalidateEvent(keys=["todos"]))

    async def linked_memory_ids(self, todo_id: UUID) -> list[str]:
        """The memory ids linked to a Todo, oldest link first."""
        async with self.database.transaction() as tx:
            links = await tx.fetch_all(
                select(TodoMemory)
                .where(TodoMemory.todo_id.eq(str(todo_id)))
                .order_by(TodoMemory.created_at.asc())
            )
        return [link.memory_id for link in links]

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
    tethered = await memory_service.browse_by_state("tethered", logger=logger)
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
        _info(logger, "Migrated pending-action facet Memories to Todos", count=migrated)
    return migrated


async def _todos_linked_to_memory(database: Database, memory_id: UUID) -> list[str]:
    """Todo ids already linked to a Memory, for the backfill's idempotency guard."""
    async with database.transaction() as tx:
        links = await tx.fetch_all(
            select(TodoMemory).where(TodoMemory.memory_id.eq(str(memory_id)))
        )
    return [link.todo_id for link in links]
