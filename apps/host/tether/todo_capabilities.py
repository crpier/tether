"""Todo capability execution and Open WebUI read models."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.structured_logging import get_request_logger
from tether.todo_errors import InvalidTodoError, TodoConflictError, TodoNotFoundError
from tether.todo_model import TodoStatus
from tether.todo_store import Todo
from tether.todos import TodoReadiness, TodoService, todo_reference


def _service(request: Request) -> TodoService:
    """Read Todo operations from the installed headless runtime."""
    return cast("TodoService", request.app.state.runtime.todo_service)


TODO_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((TodoNotFoundError,), "not_found"),
    ErrorRule((TodoConflictError,), "conflict"),
    ErrorRule((InvalidTodoError,), "invalid_input"),
)
"""Expected Todo failures translated at the Open WebUI boundary."""


class TodoRead(BaseModel):
    """Open WebUI representation of a Todo and its computed waiting state."""

    id: UUID7
    action: str
    status: TodoStatus
    condition: str | None
    waiting: bool
    version: PositiveInt
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_todo(cls, todo: Todo[Fetched], *, waiting: bool) -> TodoRead:
        """Render a stored Todo as its HTTP representation."""
        return cls(
            id=todo.id,
            action=todo.action,
            status=todo.status,
            condition=todo.condition,
            waiting=waiting,
            version=todo.version,
            created_at=todo.created_at,
            updated_at=todo.updated_at,
        )


class TodoReadinessRead(BaseModel):
    """The active Todos split into bounded ready and waiting collections."""

    ready: list[TodoRead]
    waiting: list[TodoRead]

    @classmethod
    def from_readiness(cls, readiness: TodoReadiness) -> TodoReadinessRead:
        """Render a computed readiness split as its HTTP representation."""
        return cls(
            ready=[TodoRead.from_todo(todo, waiting=False) for todo in readiness.ready],
            waiting=[
                TodoRead.from_todo(todo, waiting=True) for todo in readiness.waiting
            ],
        )


def _single(todo: Todo[Fetched]) -> CapabilityOutcome:
    """Render a single Todo outcome; readiness is not recomputed per mutation."""
    waiting = todo.condition is not None
    return CapabilityOutcome(
        result=TodoRead.from_todo(todo, waiting=waiting).model_dump(mode="json")
    )


async def create(
    request: Request,
    action: str,
    condition: str | None = None,
) -> CapabilityOutcome:
    """Create an active Todo, optionally with a free-text waiting condition."""
    todo = await _service(request).create(
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
    todo = await _service(request).set_status(
        todo_reference(todo_id, version),
        status,
        logger=get_request_logger(request),
    )
    return _single(todo)


async def list_todos(request: Request) -> CapabilityOutcome:
    """List the active Todos split into ready and waiting."""
    readiness = await _service(request).readiness(logger=get_request_logger(request))
    return CapabilityOutcome(
        result=TodoReadinessRead.from_readiness(readiness).model_dump(mode="json")
    )
