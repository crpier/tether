"""HTTP routes for the Memory Review spine.

Each FastAPI route validates its request body or query string with Pydantic,
then binds the validated
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

FastAPI derives OpenAPI from the same request and response models used at runtime.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response

from tether import memory_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.memories import MemoryNotFoundError
from tether.memory_capabilities import MEMORY_ERRORS, MemoryContent, MemoryRead
from tether.memory_store import MemoryState


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


def _path_memory_id(raw_memory_id: str) -> UUID:
    """Parse the `{memory_id}` path segment, treating a malformed id as absent."""
    try:
        return UUID(raw_memory_id)
    except ValueError as error:
        raise MemoryNotFoundError(raw_memory_id) from error


_translate_domain_errors = translate_domain_errors(MEMORY_ERRORS)


router = APIRouter()


@router.post("/api/memories", response_model=MemoryRead, status_code=201)
async def capture_memory(request: Request, body: CaptureRequest) -> Response:
    """Capture a loose Memory."""
    outcome = await memory_capabilities.capture(request, body.content)
    return rest_response(outcome, status_code=201)


@router.get("/api/memories", response_model=list[MemoryRead])
async def browse_memories(
    request: Request, query: Annotated[BrowseQuery, Query()]
) -> Response:
    """Filter the review queue (`loose`) or browse the corpus (`tethered`)."""
    return rest_response(await memory_capabilities.browse(request, query.state))


@router.get("/api/memories/search", response_model=list[MemoryRead])
@_translate_domain_errors
async def search_memories(
    request: Request, query: Annotated[SearchQuery, Query()]
) -> Response:
    """Keyword Search over tethered Memories."""
    outcome = await memory_capabilities.search(request, query.q, limit=query.limit)
    return rest_response(outcome)


@router.patch("/api/memories/{memory_id}", response_model=MemoryRead)
@_translate_domain_errors
async def edit_memory(request: Request, body: EditRequest, memory_id: str) -> Response:
    """Edit a Memory's `content`; a human edit keeps trust."""
    outcome = await memory_capabilities.edit(
        request, _path_memory_id(memory_id), body.content, body.version
    )
    return rest_response(outcome)


@router.post("/api/memories/{memory_id}/tether", response_model=MemoryRead)
@_translate_domain_errors
async def tether_memory(
    request: Request, body: TetherRequest, memory_id: str
) -> Response:
    """Promote a loose Memory to tethered."""
    outcome = await memory_capabilities.tether(
        request, _path_memory_id(memory_id), body.version
    )
    return rest_response(outcome)


@router.delete("/api/memories/{memory_id}", response_model=MemoryRead)
@_translate_domain_errors
async def reject_memory(
    request: Request, query: Annotated[RejectQuery, Query()], memory_id: str
) -> Response:
    """Soft-delete (reject) a Memory."""
    outcome = await memory_capabilities.reject(
        request, _path_memory_id(memory_id), query.version
    )
    return rest_response(outcome)


# `/api/memories/search` precedes `/api/memories/{memory_id}` so the literal path wins.
