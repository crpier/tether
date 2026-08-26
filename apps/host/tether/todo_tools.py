"""The Open WebUI Todo tool descriptors."""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt

from tether.capabilities import bind_params
from tether.todo_capabilities import (
    TODO_ERRORS,
    create,
    list_todos,
    set_status,
)
from tether.todo_model import TodoStatus
from tether.tool_runtime import ToolSpec


class CreateTodoParams(BaseModel):
    """Params for capturing a one-off actionable task.

    `condition` is an optional free-text waiting condition ("next time I visit
    Ana") for an event-dependent task; omit it for a task that is ready now.
    """

    action: str
    condition: str | None = None


class SetTodoStatusParams(BaseModel):
    """Params for transitioning a Todo's status at an observed version."""

    todo_id: UUID7
    version: PositiveInt
    status: TodoStatus


class ListTodosParams(BaseModel):
    """Params for listing the bounded active Todos by readiness."""


TODO_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("create_todo", CreateTodoParams, bind_params(create), TODO_ERRORS),
    ToolSpec(
        "set_todo_status", SetTodoStatusParams, bind_params(set_status), TODO_ERRORS
    ),
    ToolSpec("list_todos", ListTodosParams, bind_params(list_todos), TODO_ERRORS),
)
"""The Todo capabilities exposed to Open WebUI."""
