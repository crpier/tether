"""The Todo vertical: a single-action item with a computed waiting state.

A Todo is one thing to do — "bring the book next time I visit Ana", "dig out the
grey shirt before the gala", "research the pension transfer". It is distinct from
a Bucket item (an intention to *consume* something) and a Project (a multi-step
undertaking): a Todo is exactly one action, born active, and reaching a terminal
`completed` or `abandoned` state through the base-set lifecycle convention (the
status column is a plain string, not an enum enforced in the schema — ADR 0016's
bespoke idiom).

A Todo can carry an optional free-text waiting condition ("next time I visit
Ana"). The legacy `trigger_id` column remains inert until the explicit cleanup
command clears old links. Readiness never consults trigger or notification state.

>>> service = TodoService(database=database, tracer=tracer)
>>> todo = await service.create("call the dentist", logger=logger)
>>> todo.status
'active'
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.structured_logging import Logger
from tether.todo_errors import InvalidTodoError, TodoConflictError, TodoNotFoundError
from tether.todo_model import READY_DIGEST_CAP, WAITING_DIGEST_CAP, TodoStatus
from tether.todo_store import Todo


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
    """The live active Todos split into bounded ready and waiting lists.

    `ready` are actionable now and `waiting` carry an unmet free-text condition.
    Both groups preserve newest-first order and are independently bounded.
    """

    ready: list[Todo[Fetched]]
    waiting: list[Todo[Fetched]]


def todo_reference(todo_id: UUID, version: PositiveInt) -> Todo[Fetched]:
    """Build a detached Todo carrying only the identity a mutation acts on.

    Status transitions read just `id` and `version` to run their
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
    computed from each active Todo's free-text condition, never stored.
    """

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
    ) -> None:
        self.database: Database = database
        self.tracer: Tracer = tracer

    async def create(
        self,
        action: str,
        *,
        condition: str | None = None,
        logger: Logger,
    ) -> Todo[Fetched]:
        """Create an active Todo, optionally with a waiting condition."""
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
                        )
                    ).returning()
                )

            async with self.database.transaction(mode="immediate") as tx:
                todo = await _create(tx)
            span.set_attribute("todo.id", str(todo.id))
            _info(logger, "Todo created", todo_id=str(todo.id))
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
        return fresh

    async def readiness(self, *, logger: Logger) -> TodoReadiness:
        """Split the active Todos into ready and waiting, computing each.

        Waiting is derived only from the free-text condition. Legacy trigger
        links do not affect readiness. Both groups are capped for tool output.
        """
        _debug(logger, "Computing Todo readiness")
        async with self.database.transaction() as tx:
            active = await tx.fetch_all(
                select(Todo)
                .where(Todo.status.eq("active"))
                .order_by(Todo.created_at.desc())
            )

        ready: list[Todo[Fetched]] = []
        waiting: list[Todo[Fetched]] = []
        for todo in active:
            if todo.condition is not None:
                waiting.append(todo)
            else:
                ready.append(todo)
        return TodoReadiness(
            ready=ready[:READY_DIGEST_CAP],
            waiting=waiting[:WAITING_DIGEST_CAP],
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
