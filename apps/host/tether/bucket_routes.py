"""HTTP routes for Bucket items.

Each handler adapts one `BucketItemService` capability to HTTP: `endpoint`
validates the request body or query string with Pydantic, the handler calls
`request.app.state.bucket_item_service`, and the result is serialised as
`BucketItemRead` (or, for Add, `AddBucketItemResponse` — the new item plus its
dedup advisory). Domain exceptions translate to status codes at this boundary —
`BucketItemNotFoundError` -> 404, `BucketItemConflictError` -> 409,
`EmptyBucketSearchQueryError` -> 400, and a malformed payload or blank intent
context -> 422.

Complete/Delete are optimistic-concurrency checked: the client sends the
`version` it last observed (in the body for complete, the query string for
delete), and a version that has moved on (or a now-terminal item) surfaces as a
409.
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

from tether.bucket_items import (
    AddOutcome,
    BucketItem,
    BucketItemConflictError,
    BucketItemNotFoundError,
    BucketItemState,
    DedupSeverity,
    EmptyBucketSearchQueryError,
    EmptyIntentContextError,
    Fetched,
    InvalidItemDataError,
    ItemType,
    JsonValue,
    derive_state,
)
from tether.logging import Logger, get_request_logger
from tether.openapi import EndpointRoute, endpoint

type IntentContext = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AddBucketItemRequest(BaseModel):
    """Body for Adding a Bucket item under one item type.

    >>> AddBucketItemRequest(
    ...     item_type="movie", data={"title": "Dune"}, intent_context="recommended"
    ... ).item_type
    'movie'
    """

    item_type: ItemType
    data: dict[str, JsonValue]
    intent_context: IntentContext


class CompleteRequest(BaseModel):
    """Body for completing a Bucket item at an observed `version`.

    >>> CompleteRequest(version=1).version
    1
    """

    version: PositiveInt


class DeleteQuery(BaseModel):
    """Query string carrying the `version` a delete targets.

    >>> DeleteQuery(version=1).version
    1
    """

    version: PositiveInt


class BrowseQuery(BaseModel):
    """Query string for the active list / retained-history browse.

    >>> BrowseQuery(state="active").state
    'active'
    """

    state: BucketItemState


class SearchQuery(BaseModel):
    """Query string for keyword Search over active Bucket items.

    >>> SearchQuery(q="Dune").limit
    50
    """

    limit: PositiveInt = 50
    q: str


class BucketItemRead(BaseModel):
    """HTTP representation of a Bucket item, exposing its derived `state`.

    >>> read = BucketItemRead(
    ...     id="018f0000-0000-7000-8000-000000000000",
    ...     item_type="movie",
    ...     state="active",
    ...     title="Dune",
    ...     data={"title": "Dune"},
    ...     intent_context="recommended",
    ...     version=1,
    ...     created_at=datetime(2026, 1, 1),
    ...     updated_at=datetime(2026, 1, 1),
    ...     completed_at=None,
    ...     deleted_at=None,
    ... )
    >>> read.state
    'active'
    """

    id: UUID7
    item_type: ItemType
    state: BucketItemState
    title: str
    data: dict[str, JsonValue]
    intent_context: str
    version: PositiveInt
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    deleted_at: datetime | None

    @classmethod
    def from_item(cls, item: BucketItem[Fetched]) -> BucketItemRead:
        """Render a stored Bucket item as its HTTP representation.

        `state` is derived, not stored: a stamped terminal timestamp names the
        terminal state, otherwise the item is active.
        """
        return cls(
            id=item.id,
            item_type=item.item_type,
            state=derive_state(item),
            title=item.title,
            data=item.data,
            intent_context=item.intent_context,
            version=item.version,
            created_at=item.created_at,
            updated_at=item.updated_at,
            completed_at=item.completed_at,
            deleted_at=item.deleted_at,
        )


class DedupAdvisoryRead(BaseModel):
    """The dedup advisory returned alongside a freshly Added item.

    `severity` is `warn` when an active duplicate already exists, `inform` when
    the only duplicates are terminal (completed/deleted), `none` otherwise.
    Dedup never blocks, so the item is Added regardless.
    """

    severity: DedupSeverity
    duplicates: list[BucketItemRead]


class AddBucketItemResponse(BaseModel):
    """The Add result: the new item plus its dedup advisory."""

    item: BucketItemRead
    dedup: DedupAdvisoryRead

    @classmethod
    def from_outcome(cls, outcome: AddOutcome) -> AddBucketItemResponse:
        """Render an `AddOutcome` as its HTTP representation."""
        return cls(
            item=BucketItemRead.from_item(outcome.item),
            dedup=DedupAdvisoryRead(
                severity=outcome.severity,
                duplicates=[
                    BucketItemRead.from_item(duplicate)
                    for duplicate in outcome.duplicates
                ],
            ),
        )


def _bucket_item_reference(
    bucket_item_id: UUID, version: PositiveInt
) -> BucketItem[Fetched]:
    """Build a detached Bucket item carrying only the identity a mutation acts on.

    Complete/Delete read just `id` and `version` to run their optimistic-
    concurrency check and re-fetch the live row, so a hand-built reference is
    enough; the other columns are required but play no role on this path.
    """
    return cast(
        "BucketItem[Fetched]",
        BucketItem.construct(
            id=bucket_item_id,
            version=version,
            item_type="movie",
            title="",
            dedup_key="",
            data={},
            intent_context="",
        ),
    )


def _path_bucket_item_id(request: Request) -> UUID:
    """Parse the `{bucket_item_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["bucket_item_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise BucketItemNotFoundError(raw_id) from error


def _read_response(
    item: BucketItem[Fetched], *, status_code: int = 200
) -> JSONResponse:
    """Serialise a stored Bucket item as its `BucketItemRead` JSON body."""
    return JSONResponse(
        BucketItemRead.from_item(item).model_dump(mode="json"),
        status_code=status_code,
    )


def _list_response(items: list[BucketItem[Fetched]]) -> JSONResponse:
    """Serialise a Bucket item collection as a `BucketItemRead` JSON array."""
    return JSONResponse(
        [BucketItemRead.from_item(item).model_dump(mode="json") for item in items]
    )


def _request_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _translate_domain_errors(
    handler: Callable[..., Awaitable[Response]],
) -> Callable[..., Awaitable[Response]]:
    """Map Bucket item domain failures onto HTTP status codes at the boundary.

    Absence (including a malformed id) is a 404; a domain-state or stale-version
    conflict is a 409; a blank keyword query is a 400; a malformed payload or
    blank intent context is a 422.
    """

    @functools.wraps(handler)
    async def translated(*arguments: object) -> Response:
        try:
            return await handler(*arguments)
        except BucketItemNotFoundError:
            return JSONResponse({"detail": "bucket item not found"}, status_code=404)
        except BucketItemConflictError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except EmptyBucketSearchQueryError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        except (InvalidItemDataError, EmptyIntentContextError) as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    return translated


@endpoint(request_body=AddBucketItemRequest, response=AddBucketItemResponse, status=201)
@_translate_domain_errors
async def add_bucket_item(request: Request, body: AddBucketItemRequest) -> Response:
    """Add a Bucket item; the response carries its dedup advisory."""
    outcome = await request.app.state.bucket_item_service.add(
        body.item_type,
        body.data,
        body.intent_context,
        logger=_request_logger(request),
    )
    return JSONResponse(
        AddBucketItemResponse.from_outcome(outcome).model_dump(mode="json"),
        status_code=201,
    )


@endpoint(query=BrowseQuery, response=BucketItemRead, response_is_list=True)
async def browse_bucket_items(request: Request, query: BrowseQuery) -> Response:
    """List Bucket items in a lifecycle state (active list / retained history)."""
    items = await request.app.state.bucket_item_service.browse_by_state(
        query.state,
        logger=_request_logger(request),
    )
    return _list_response(items)


@endpoint(query=SearchQuery, response=BucketItemRead, response_is_list=True)
@_translate_domain_errors
async def search_bucket_items(request: Request, query: SearchQuery) -> Response:
    """Keyword Search over active Bucket items."""
    items = await request.app.state.bucket_item_service.search(
        query.q,
        limit=query.limit,
        logger=_request_logger(request),
    )
    return _list_response(items)


@endpoint(request_body=CompleteRequest, response=BucketItemRead)
@_translate_domain_errors
async def complete_bucket_item(request: Request, body: CompleteRequest) -> Response:
    """Complete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.complete(
        _bucket_item_reference(_path_bucket_item_id(request), body.version),
        logger=_request_logger(request),
    )
    return _read_response(item)


@endpoint(query=DeleteQuery, response=BucketItemRead)
@_translate_domain_errors
async def delete_bucket_item(request: Request, query: DeleteQuery) -> Response:
    """Delete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.delete(
        _bucket_item_reference(_path_bucket_item_id(request), query.version),
        logger=_request_logger(request),
    )
    return _read_response(item)


# `/bucket-items/search` precedes `/bucket-items/{bucket_item_id}` so the
# literal path wins.
bucket_item_routes: list[Route] = [
    EndpointRoute("/bucket-items", add_bucket_item, methods=["POST"]),
    EndpointRoute("/bucket-items", browse_bucket_items, methods=["GET"]),
    EndpointRoute("/bucket-items/search", search_bucket_items, methods=["GET"]),
    EndpointRoute(
        "/bucket-items/{bucket_item_id}", delete_bucket_item, methods=["DELETE"]
    ),
    EndpointRoute(
        "/bucket-items/{bucket_item_id}/complete",
        complete_bucket_item,
        methods=["POST"],
    ),
]
