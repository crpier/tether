"""HTTP routes for the Memory Review spine.

Each route adapts one Memory capability to HTTP: `endpoint` validates the
request body or query string with Pydantic, the handler binds the validated
input (plus any path id) onto the capability execute in
`tether.memory_capabilities`, and the outcome is served as `MemoryRead` JSON.
Domain exceptions translate to status codes through the domain's `ErrorRule`
table (`MEMORY_ERRORS`) — absence -> 404, conflict -> 409, blank query -> 400 —
the same table the internal tool surface maps onto envelope codes.

Mutations are optimistic-concurrency checked: the client sends the `version` it
last observed (in the body for edit/tether, the query string for reject), and a
version that has moved on surfaces as a 409. The capability packages the path
id and that version into a detached `Memory` reference for the service, which
owns the row lookup and the conflict decision.

The same `endpoint` decoration records each handler's request/response model so
`build_openapi` can describe the API without a second source of truth.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import memory_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.memories import MemoryNotFoundError, MemoryState
from tether.memory_capabilities import MEMORY_ERRORS, MemoryContent, MemoryRead
from tether.openapi import EndpointRoute, endpoint


class CaptureRequest(BaseModel):
    """Body for capturing a loose Memory.

    >>> CaptureRequest(content="I prefer aisle seats").content
    'I prefer aisle seats'
    """

    content: MemoryContent


class EditRequest(BaseModel):
    """Body for editing a Memory's content at an observed `version`.

    >>> EditRequest(content="I prefer window seats", version=1).version
    1
    """

    content: MemoryContent
    version: PositiveInt


class TetherRequest(BaseModel):
    """Body for tethering a Memory at an observed `version`.

    >>> TetherRequest(version=1).version
    1
    """

    version: PositiveInt


class RejectQuery(BaseModel):
    """Query string carrying the `version` a reject targets.

    >>> RejectQuery(version=1).version
    1
    """

    version: PositiveInt


class BrowseQuery(BaseModel):
    """Query string for the human review queue / corpus browse.

    >>> BrowseQuery(state="loose").state
    'loose'
    """

    state: MemoryState


class SearchQuery(BaseModel):
    """Query string for the assistant's keyword Search.

    >>> SearchQuery(q="aisle").limit
    50
    """

    limit: PositiveInt = 50
    q: str


def _path_memory_id(request: Request) -> UUID:
    """Parse the `{memory_id}` path segment, treating a malformed id as absent."""
    raw_memory_id = request.path_params["memory_id"]
    try:
        return UUID(raw_memory_id)
    except ValueError as error:
        raise MemoryNotFoundError(raw_memory_id) from error


_translate_domain_errors = translate_domain_errors(MEMORY_ERRORS)


@endpoint(request_body=CaptureRequest, response=MemoryRead, status=201)
async def capture_memory(request: Request, body: CaptureRequest) -> Response:
    """Capture a loose Memory."""
    outcome = await memory_capabilities.capture(request, body.content)
    return rest_response(outcome, status_code=201)


@endpoint(query=BrowseQuery, response=MemoryRead, response_is_list=True)
async def browse_memories(request: Request, query: BrowseQuery) -> Response:
    """Filter the review queue (`loose`) or browse the corpus (`tethered`)."""
    return rest_response(await memory_capabilities.browse(request, query.state))


@endpoint(query=SearchQuery, response=MemoryRead, response_is_list=True)
@_translate_domain_errors
async def search_memories(request: Request, query: SearchQuery) -> Response:
    """Keyword Search over tethered Memories."""
    outcome = await memory_capabilities.search(request, query.q, limit=query.limit)
    return rest_response(outcome)


@endpoint(request_body=EditRequest, response=MemoryRead)
@_translate_domain_errors
async def edit_memory(request: Request, body: EditRequest) -> Response:
    """Edit a Memory's `content`; a human edit keeps trust."""
    outcome = await memory_capabilities.edit(
        request, _path_memory_id(request), body.content, body.version
    )
    return rest_response(outcome)


@endpoint(request_body=TetherRequest, response=MemoryRead)
@_translate_domain_errors
async def tether_memory(request: Request, body: TetherRequest) -> Response:
    """Promote a loose Memory to tethered."""
    outcome = await memory_capabilities.tether(
        request, _path_memory_id(request), body.version
    )
    return rest_response(outcome)


@endpoint(query=RejectQuery, response=MemoryRead)
@_translate_domain_errors
async def reject_memory(request: Request, query: RejectQuery) -> Response:
    """Soft-delete (reject) a Memory."""
    outcome = await memory_capabilities.reject(
        request, _path_memory_id(request), query.version
    )
    return rest_response(outcome)


# `/api/memories/search` precedes `/api/memories/{memory_id}` so the literal path wins.
routes: list[Route] = [
    EndpointRoute("/api/memories", capture_memory, methods=["POST"]),
    EndpointRoute("/api/memories", browse_memories, methods=["GET"]),
    EndpointRoute("/api/memories/search", search_memories, methods=["GET"]),
    EndpointRoute("/api/memories/{memory_id}", edit_memory, methods=["PATCH"]),
    EndpointRoute("/api/memories/{memory_id}", reject_memory, methods=["DELETE"]),
    EndpointRoute("/api/memories/{memory_id}/tether", tether_memory, methods=["POST"]),
]
