"""HTTP routes for the Memory Review spine.

Each handler adapts one Memory service capability to HTTP: `endpoint` validates
the request body or query string with Pydantic, the handler calls
`request.app.state.memory_service`, and the resulting Memory is serialised as
`MemoryRead`. Domain exceptions translate to status codes at this boundary —
`MemoryNotFoundError` -> 404, `MemoryConflictError` -> 409,
`EmptySearchQueryError` -> 400.

Mutations are optimistic-concurrency checked: the client sends the `version` it
last observed (in the body for edit/tether, the query string for reject), and a
version that has moved on surfaces as a 409. Each handler packages the path id
and that version into a detached `Memory` reference for the service, which owns
the row lookup and the conflict decision.

The same `endpoint` decoration records each handler's request/response model so
`build_openapi` can describe the API without a second source of truth.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.memories import (
    EmptySearchQueryError,
    Fetched,
    Memory,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryState,
)
from tether.openapi import EndpointRoute, endpoint

type MemoryContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


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


class MemoryRead(BaseModel):
    """HTTP representation of a Memory, exposing its derived trust `state`.

    >>> read = MemoryRead(
    ...     content="I prefer aisle seats",
    ...     created_at=datetime(2026, 1, 1),
    ...     id="018f0000-0000-7000-8000-000000000000",
    ...     state="loose",
    ...     tethered_at=None,
    ...     updated_at=datetime(2026, 1, 1),
    ...     version=1,
    ... )
    >>> read.state
    'loose'
    """

    content: str
    created_at: datetime
    id: UUID7
    state: MemoryState
    tethered_at: datetime | None
    updated_at: datetime
    version: PositiveInt

    @classmethod
    def from_memory(cls, memory: Memory[Fetched]) -> MemoryRead:
        """Render a stored Memory as its HTTP representation.

        A Memory's `state` is derived, not stored: a stamped `tethered_at`
        means a human has vetted it, so it reads as `tethered`.
        """
        return cls(
            content=memory.content,
            created_at=memory.created_at,
            id=memory.id,
            state="tethered" if memory.tethered_at is not None else "loose",
            tethered_at=memory.tethered_at,
            updated_at=memory.updated_at,
            version=memory.version,
        )


def _memory_reference(memory_id: UUID, version: PositiveInt) -> Memory[Fetched]:
    """Build a detached Memory carrying only the identity a mutation acts on.

    The service's tether/edit/delete read just `id` and `version` to run their
    optimistic-concurrency check and then re-fetch the live row, so a hand-built
    reference is enough. `content` is a required column with no role on this
    path, hence the empty placeholder.
    """
    return cast(
        "Memory[Fetched]",
        Memory.construct(content="", id=memory_id, version=version),
    )


def _path_memory_id(request: Request) -> UUID:
    """Parse the `{memory_id}` path segment, treating a malformed id as absent."""
    raw_memory_id = request.path_params["memory_id"]
    try:
        return UUID(raw_memory_id)
    except ValueError as error:
        raise MemoryNotFoundError(raw_memory_id) from error


def _read_response(memory: Memory[Fetched], *, status_code: int = 200) -> JSONResponse:
    """Serialise a stored Memory as its `MemoryRead` JSON body."""
    return JSONResponse(
        MemoryRead.from_memory(memory).model_dump(mode="json"),
        status_code=status_code,
    )


def _list_response(memories: list[Memory[Fetched]]) -> JSONResponse:
    """Serialise a Memory collection as a `MemoryRead` JSON array."""
    return JSONResponse(
        [MemoryRead.from_memory(memory).model_dump(mode="json") for memory in memories]
    )


def _request_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _translate_domain_errors(
    handler: Callable[..., Awaitable[Response]],
) -> Callable[..., Awaitable[Response]]:
    """Map Memory domain failures onto HTTP status codes at the route boundary.

    Absence (including a malformed id) is a 404; a domain-state or stale-version
    conflict is a 409; a blank keyword query is a 400. Wrapping the handler keeps
    each route body focused on the happy path.
    """

    @functools.wraps(handler)
    async def translated(*arguments: object) -> Response:
        try:
            return await handler(*arguments)
        except MemoryNotFoundError:
            return JSONResponse({"detail": "memory not found"}, status_code=404)
        except MemoryConflictError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except EmptySearchQueryError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

    return translated


@endpoint(request_body=CaptureRequest, response=MemoryRead, status=201)
async def capture_memory(request: Request, body: CaptureRequest) -> Response:
    """Capture a loose Memory."""
    memory = await request.app.state.memory_service.capture(
        body.content,
        logger=_request_logger(request),
    )
    return _read_response(memory, status_code=201)


@endpoint(query=BrowseQuery, response=MemoryRead, response_is_list=True)
async def browse_memories(request: Request, query: BrowseQuery) -> Response:
    """Filter the review queue (`loose`) or browse the corpus (`tethered`)."""
    memories = await request.app.state.memory_service.browse_by_state(
        query.state,
        logger=_request_logger(request),
    )
    return _list_response(memories)


@endpoint(query=SearchQuery, response=MemoryRead, response_is_list=True)
@_translate_domain_errors
async def search_memories(request: Request, query: SearchQuery) -> Response:
    """Keyword Search over tethered Memories."""
    memories = await request.app.state.memory_service.search(
        query.q,
        limit=query.limit,
        logger=_request_logger(request),
    )
    return _list_response(memories)


@endpoint(request_body=EditRequest, response=MemoryRead)
@_translate_domain_errors
async def edit_memory(request: Request, body: EditRequest) -> Response:
    """Edit a Memory's `content`; a human edit keeps trust."""
    memory = await request.app.state.memory_service.edit_content(
        _memory_reference(_path_memory_id(request), body.version),
        body.content,
        logger=_request_logger(request),
    )
    return _read_response(memory)


@endpoint(request_body=TetherRequest, response=MemoryRead)
@_translate_domain_errors
async def tether_memory(request: Request, body: TetherRequest) -> Response:
    """Promote a loose Memory to tethered."""
    memory = await request.app.state.memory_service.tether(
        _memory_reference(_path_memory_id(request), body.version),
        logger=_request_logger(request),
    )
    return _read_response(memory)


@endpoint(query=RejectQuery, response=MemoryRead)
@_translate_domain_errors
async def reject_memory(request: Request, query: RejectQuery) -> Response:
    """Soft-delete (reject) a Memory."""
    memory = await request.app.state.memory_service.delete(
        _memory_reference(_path_memory_id(request), query.version),
        logger=_request_logger(request),
    )
    return _read_response(memory)


# `/memories/search` precedes `/memories/{memory_id}` so the literal path wins.
routes: list[Route] = [
    EndpointRoute("/memories", capture_memory, methods=["POST"]),
    EndpointRoute("/memories", browse_memories, methods=["GET"]),
    EndpointRoute("/memories/search", search_memories, methods=["GET"]),
    EndpointRoute("/memories/{memory_id}", edit_memory, methods=["PATCH"]),
    EndpointRoute("/memories/{memory_id}", reject_memory, methods=["DELETE"]),
    EndpointRoute("/memories/{memory_id}/tether", tether_memory, methods=["POST"]),
]
