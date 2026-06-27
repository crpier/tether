"""The internal Bucket item tool surface, over the shared response envelope.

These mount alongside the Memory tools under `/internal/tools/*` — the loopback
seam a pi process calls back into — reusing the same auth gate, params-to-
envelope validation, and domain-error translation (`tether.tools`).

Bucket items are typed, so Add is exposed per item type (`add_movie`,
`add_place`): each tool takes that type's own flat fields, keeping the surface
friendly to a weak model rather than asking it to assemble a polymorphic JSON
payload. The richer/optional fields and a single generic Add live on the REST
surface. Complete, Delete, and Search round out the belt.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether.bucket_items import AddOutcome, BucketItem, Fetched
from tether.bucket_routes import AddBucketItemResponse, BucketItemRead
from tether.logging import Logger, get_request_logger
from tether.tools import ToolEndpoint, ToolEnvelope, ToolRoute


class AddMovieParams(BaseModel):
    """Params for Adding a `movie` Bucket item."""

    title: str
    intent_context: str
    year: int | None = None


class AddPlaceParams(BaseModel):
    """Params for Adding a `place` Bucket item."""

    name: str
    intent_context: str
    location: str | None = None


class CompleteBucketItemParams(BaseModel):
    """Params for completing a Bucket item at an observed version."""

    bucket_item_id: UUID7
    version: PositiveInt


class DeleteBucketItemParams(BaseModel):
    """Params for deleting a Bucket item at an observed version."""

    bucket_item_id: UUID7
    version: PositiveInt


class SearchBucketItemsParams(BaseModel):
    """Params for keyword Search over active Bucket items."""

    q: str
    limit: PositiveInt = 50


def _bucket_item_reference(
    bucket_item_id: UUID7, version: PositiveInt
) -> BucketItem[Fetched]:
    """Build a detached Bucket item carrying only the identity a mutation acts on.

    Complete/Delete read just `id` and `version` to run their optimistic-
    concurrency check and re-fetch the live row, so the other columns are
    required placeholders with no role here.
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


def _ok_add_outcome(outcome: AddOutcome) -> ToolEnvelope:
    """Envelope an Add result: the new item plus its dedup advisory."""
    return ToolEnvelope(
        success=True,
        result=AddBucketItemResponse.from_outcome(outcome).model_dump(mode="json"),
        provenance=outcome.item.provenance,
    )


def _ok_bucket_item(item: BucketItem[Fetched]) -> ToolEnvelope:
    """Envelope a single-item result, surfacing its provenance."""
    return ToolEnvelope(
        success=True,
        result=BucketItemRead.from_item(item).model_dump(mode="json"),
        provenance=item.provenance,
    )


def _ok_bucket_items(items: list[BucketItem[Fetched]]) -> ToolEnvelope:
    """Envelope a Bucket item collection; provenance is null for collections."""
    return ToolEnvelope(
        success=True,
        result=[
            BucketItemRead.from_item(item).model_dump(mode="json") for item in items
        ],
    )


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


async def _add_movie(request: Request, params: AddMovieParams) -> ToolEnvelope:
    """Add a `movie` Bucket item."""
    data: dict[str, Any] = {"title": params.title}
    if params.year is not None:
        data["year"] = params.year
    outcome = await request.app.state.bucket_item_service.add(
        "movie", data, params.intent_context, logger=_tool_logger(request)
    )
    return _ok_add_outcome(outcome)


async def _add_place(request: Request, params: AddPlaceParams) -> ToolEnvelope:
    """Add a `place` Bucket item."""
    data: dict[str, Any] = {"name": params.name}
    if params.location is not None:
        data["location"] = params.location
    outcome = await request.app.state.bucket_item_service.add(
        "place", data, params.intent_context, logger=_tool_logger(request)
    )
    return _ok_add_outcome(outcome)


async def _complete(request: Request, params: CompleteBucketItemParams) -> ToolEnvelope:
    """Complete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.complete(
        _bucket_item_reference(params.bucket_item_id, params.version),
        logger=_tool_logger(request),
    )
    return _ok_bucket_item(item)


async def _delete(request: Request, params: DeleteBucketItemParams) -> ToolEnvelope:
    """Delete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.delete(
        _bucket_item_reference(params.bucket_item_id, params.version),
        logger=_tool_logger(request),
    )
    return _ok_bucket_item(item)


async def _search(request: Request, params: SearchBucketItemsParams) -> ToolEnvelope:
    """Keyword Search over active Bucket items."""
    items = await request.app.state.bucket_item_service.search(
        params.q, limit=params.limit, logger=_tool_logger(request)
    )
    return _ok_bucket_items(items)


def internal_bucket_tool_routes() -> list[Route]:
    """Mount the Bucket item capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Bucket routes (and the Memory tools) so
    they stay absent from the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/add_movie",
            ToolEndpoint(AddMovieParams, _add_movie),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/add_place",
            ToolEndpoint(AddPlaceParams, _add_place),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/complete_bucket_item",
            ToolEndpoint(CompleteBucketItemParams, _complete),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/delete_bucket_item",
            ToolEndpoint(DeleteBucketItemParams, _delete),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/search_bucket_items",
            ToolEndpoint(SearchBucketItemsParams, _search),
            methods=["POST"],
        ),
    ]
