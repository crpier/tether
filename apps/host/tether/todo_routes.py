"""HTTP routes for Todos.

The panel reads the active Todos (`GET /api/todos`, split into ready and waiting)
and transitions status (`POST /api/todos/{todo_id}/status`) — the only write the
panel performs; chat authors everything else. Domain exceptions translate to
status codes through the domain's `ErrorRule` table (`TODO_ERRORS`) — absence ->
404, stale version -> 409, blank action -> 422 — the same table the internal tool
surface maps onto envelope codes.

The status transition is optimistic-concurrency checked: the client sends the
`version` it last observed, and a version that has moved on surfaces as a 409.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import todo_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint
from tether.todo_capabilities import TODO_ERRORS, TodoRead, TodoReadinessRead
from tether.todos import TodoNotFoundError, TodoStatus


class SetTodoStatusRequest(BaseModel):
    """Body for transitioning a Todo's status at an observed `version`.

    >>> SetTodoStatusRequest(status="completed", version=1).status
    'completed'
    """

    status: TodoStatus
    version: PositiveInt


def _path_todo_id(request: Request) -> UUID:
    """Parse the `{todo_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["todo_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise TodoNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(TODO_ERRORS)


@endpoint(response=TodoReadinessRead)
async def list_todos(request: Request) -> Response:
    """List the active Todos split into ready and waiting."""
    return rest_response(await todo_capabilities.list_todos(request))


@endpoint(request_body=SetTodoStatusRequest, response=TodoRead)
@_translate_domain_errors
async def set_todo_status(request: Request, body: SetTodoStatusRequest) -> Response:
    """Transition a Todo to a new status at an observed version."""
    outcome = await todo_capabilities.set_status(
        request, _path_todo_id(request), body.version, body.status
    )
    return rest_response(outcome)


todo_routes: list[Route] = [
    EndpointRoute("/api/todos", list_todos, methods=["GET"]),
    EndpointRoute("/api/todos/{todo_id}/status", set_todo_status, methods=["POST"]),
]
