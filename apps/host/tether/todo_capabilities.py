"""The Todo domain's capability descriptor.

The pieces the REST routes (`tether.todo_routes`) and the internal tools
(`tether.todo_tools`) both need live here once: the Read models, the
detached-reference builder, the domain→code map (`TODO_ERRORS`), and one execute
function per capability — the service call plus its Read-model rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.todos import (
    Fetched,
    InvalidTodoError,
    Todo,
    TodoConflictError,
    TodoNotFoundError,
    TodoReadiness,
    TodoStatus,
    todo_reference,
)

TODO_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((TodoNotFoundError,), "not_found", 404, detail="todo not found"),
    ErrorRule((TodoConflictError,), "conflict", 409),
    ErrorRule((InvalidTodoError,), "invalid_input", 422),
)
"""The Todo domain→code map both surfaces translate failures through."""


class TodoRead(BaseModel):
    """HTTP representation of a Todo, carrying its computed waiting state.

    `waiting` is derived (an unmet condition or an unfired trigger), never
    stored; `deadline` is the linked trigger's next fire time when it is still
    pending, else null.
    """

    id: UUID7
    action: str
    status: TodoStatus
    condition: str | None
    trigger_id: str | None
    waiting: bool
    deadline: datetime | None
    version: PositiveInt
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_todo(
        cls, todo: Todo[Fetched], *, waiting: bool, deadline: datetime | None
    ) -> TodoRead:
        """Render a stored Todo as its HTTP representation."""
        return cls(
            id=todo.id,
            action=todo.action,
            status=todo.status,
            condition=todo.condition,
            trigger_id=todo.trigger_id,
            waiting=waiting,
            deadline=deadline,
            version=todo.version,
            created_at=todo.created_at,
            updated_at=todo.updated_at,
        )


class TodoReadinessRead(BaseModel):
    """The active Todos split into ready and waiting for the panel and digest."""

    ready: list[TodoRead]
    waiting: list[TodoRead]

    @classmethod
    def from_readiness(cls, readiness: TodoReadiness) -> TodoReadinessRead:
        """Render a computed readiness split as its HTTP representation."""
        return cls(
            ready=[
                TodoRead.from_todo(todo, waiting=False, deadline=None)
                for todo in readiness.ready
            ],
            waiting=[
                TodoRead.from_todo(
                    todo, waiting=True, deadline=readiness.deadlines.get(todo.id)
                )
                for todo in readiness.waiting
            ],
        )


def _single(todo: Todo[Fetched]) -> CapabilityOutcome:
    """Render a single Todo outcome; readiness is not recomputed per mutation."""
    waiting = todo.condition is not None or todo.trigger_id is not None
    return CapabilityOutcome(
        result=TodoRead.from_todo(todo, waiting=waiting, deadline=None).model_dump(
            mode="json"
        )
    )


async def create(
    request: Request,
    action: str,
    condition: str | None = None,
) -> CapabilityOutcome:
    """Create an active Todo, optionally with a free-text waiting condition."""
    todo = await request.app.state.todo_service.create(
        action, condition=condition, logger=get_request_logger(request)
    )
    return _single(todo)


async def set_status(
    request: Request,
    todo_id: UUID,
    version: PositiveInt,
    status: TodoStatus,
) -> CapabilityOutcome:
    """Transition a Todo to a new status at an observed version."""
    todo = await request.app.state.todo_service.set_status(
        todo_reference(todo_id, version),
        status,
        logger=get_request_logger(request),
    )
    return _single(todo)


async def link_trigger(
    request: Request,
    todo_id: UUID,
    version: PositiveInt,
    trigger_id: UUID,
) -> CapabilityOutcome:
    """Attach a scheduled trigger (a deadline) to a Todo at its version."""
    todo = await request.app.state.todo_service.link_trigger(
        todo_reference(todo_id, version),
        str(trigger_id),
        logger=get_request_logger(request),
    )
    return _single(todo)


async def link_memory(
    request: Request,
    todo_id: UUID,
    memory_id: UUID,
) -> CapabilityOutcome:
    """Link a Memory to a Todo so its context travels with the task."""
    await request.app.state.todo_service.link_memory(
        todo_id, memory_id, logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result={"todo_id": str(todo_id), "memory_id": str(memory_id), "linked": True}
    )


async def list_todos(request: Request) -> CapabilityOutcome:
    """List the active Todos split into ready and waiting."""
    readiness = await request.app.state.todo_service.readiness(
        now=datetime.now(UTC), logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result=TodoReadinessRead.from_readiness(readiness).model_dump(mode="json")
    )
