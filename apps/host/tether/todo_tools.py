"""The internal Todo tool surface, over the shared response envelope.

These mount alongside the Memory, Bucket, and trigger tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and rule-driven domain-error
translation (`tether.tools`). The capability executes live in
`tether.todo_capabilities`, shared with the REST routes; this module only names
each tool's params model and mounts it.

The agent captures a one-off task (`create_todo`), settles it (`set_todo_status`),
attaches a deadline or context (`link_todo_trigger`, `link_todo_memory`), and
reads what is on the plate (`list_todos`). The panel only ever transitions
status; chat authors everything else.
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.todo_capabilities import (
    TODO_ERRORS,
    create,
    link_memory,
    link_trigger,
    list_todos,
    set_status,
)
from tether.todos import TodoStatus
from tether.tools import ToolSpec


class CreateTodoParams(BaseModel):
    """Params for capturing a one-off actionable task.

    `condition` is an optional free-text waiting condition ("next time I visit
    Ana") for an event-triggered task that resists a fixed date; omit it for a
    task that is ready now. Attach a deadline trigger afterward with
    `link_todo_trigger`.
    """

    action: str
    condition: str | None = None


class SetTodoStatusParams(BaseModel):
    """Params for transitioning a Todo's status at an observed version."""

    todo_id: UUID7
    version: PositiveInt
    status: TodoStatus


class LinkTodoTriggerParams(BaseModel):
    """Params for attaching a scheduled trigger (a deadline) to a Todo."""

    todo_id: UUID7
    version: PositiveInt
    trigger_id: UUID7


class LinkTodoMemoryParams(BaseModel):
    """Params for linking a Memory's context to a Todo."""

    todo_id: UUID7
    memory_id: UUID7


class ListTodosParams(BaseModel):
    """Params for listing the active Todos, split into ready and waiting.

    Read-only; takes no inputs beyond the session identity the gate requires.
    """


TODO_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("create_todo", CreateTodoParams, bind_params(create), TODO_ERRORS),
    ToolSpec(
        "set_todo_status", SetTodoStatusParams, bind_params(set_status), TODO_ERRORS
    ),
    ToolSpec(
        "link_todo_trigger",
        LinkTodoTriggerParams,
        bind_params(link_trigger),
        TODO_ERRORS,
    ),
    ToolSpec(
        "link_todo_memory", LinkTodoMemoryParams, bind_params(link_memory), TODO_ERRORS
    ),
    ToolSpec("list_todos", ListTodosParams, bind_params(list_todos), TODO_ERRORS),
)
"""The Todo capabilities exposed as internal tools, in generated order."""


def internal_todo_tool_routes() -> list[Route]:
    """Mount the Todo capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Todo routes so they stay absent from the
    public OpenAPI document and generated client.
    """
    return [spec.route() for spec in TODO_TOOL_SPECS]
