"""HTTP routes for Bucket items.

Each route adapts one Bucket item capability to HTTP: `endpoint` validates the
request body or query string with Pydantic, the handler binds the validated
input (plus any path id) onto the capability execute in
`tether.bucket_capabilities`, and the outcome is served as `BucketItemRead`
JSON (or, for Add, `AddBucketItemResponse` — the new item plus its dedup
advisory). Domain exceptions translate to status codes through the domain's
`ErrorRule` table (`BUCKET_ERRORS`) — absence -> 404, conflict -> 409, blank
query -> 400, malformed payload or blank intent context -> 422 — the same table
the internal tool surface maps onto envelope codes.

Complete/Delete are optimistic-concurrency checked: the client sends the
`version` it last observed (in the body for complete, the query string for
delete), and a version that has moved on (or a now-terminal item) surfaces as a
409.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, PositiveInt, StringConstraints
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import bucket_capabilities
from tether.bucket_capabilities import (
    BUCKET_ERRORS,
    AddBucketItemResponse,
    BucketItemRead,
)
from tether.bucket_items import (
    BucketItemNotFoundError,
    BucketItemState,
    ItemType,
    JsonValue,
)
from tether.capabilities import rest_response, translate_domain_errors
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


def _path_bucket_item_id(request: Request) -> UUID:
    """Parse the `{bucket_item_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["bucket_item_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise BucketItemNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(BUCKET_ERRORS)


@endpoint(request_body=AddBucketItemRequest, response=AddBucketItemResponse, status=201)
@_translate_domain_errors
async def add_bucket_item(request: Request, body: AddBucketItemRequest) -> Response:
    """Add a Bucket item; the response carries its dedup advisory."""
    outcome = await bucket_capabilities.add(
        request, body.item_type, body.data, body.intent_context
    )
    return rest_response(outcome, status_code=201)


@endpoint(query=BrowseQuery, response=BucketItemRead, response_is_list=True)
async def browse_bucket_items(request: Request, query: BrowseQuery) -> Response:
    """List Bucket items in a lifecycle state (active list / retained history)."""
    return rest_response(await bucket_capabilities.browse(request, query.state))


@endpoint(query=SearchQuery, response=BucketItemRead, response_is_list=True)
@_translate_domain_errors
async def search_bucket_items(request: Request, query: SearchQuery) -> Response:
    """Keyword Search over active Bucket items."""
    outcome = await bucket_capabilities.search(request, query.q, limit=query.limit)
    return rest_response(outcome)


@endpoint(request_body=CompleteRequest, response=BucketItemRead)
@_translate_domain_errors
async def complete_bucket_item(request: Request, body: CompleteRequest) -> Response:
    """Complete a Bucket item, moving it to terminal history."""
    outcome = await bucket_capabilities.complete(
        request, _path_bucket_item_id(request), body.version
    )
    return rest_response(outcome)


@endpoint(query=DeleteQuery, response=BucketItemRead)
@_translate_domain_errors
async def delete_bucket_item(request: Request, query: DeleteQuery) -> Response:
    """Delete a Bucket item, moving it to terminal history."""
    outcome = await bucket_capabilities.delete(
        request, _path_bucket_item_id(request), query.version
    )
    return rest_response(outcome)


# `/api/bucket-items/search` precedes `/api/bucket-items/{bucket_item_id}` so
# the literal path wins.
bucket_item_routes: list[Route] = [
    EndpointRoute("/api/bucket-items", add_bucket_item, methods=["POST"]),
    EndpointRoute("/api/bucket-items", browse_bucket_items, methods=["GET"]),
    EndpointRoute("/api/bucket-items/search", search_bucket_items, methods=["GET"]),
    EndpointRoute(
        "/api/bucket-items/{bucket_item_id}",
        delete_bucket_item,
        methods=["DELETE"],
    ),
    EndpointRoute(
        "/api/bucket-items/{bucket_item_id}/complete",
        complete_bucket_item,
        methods=["POST"],
    ),
]
