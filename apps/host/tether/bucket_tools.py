"""The internal Bucket item tool surface, over the shared response envelope.

These mount alongside the Memory tools under `/internal/tools/*` — the loopback
seam a pi process calls back into — reusing the same auth gate, params-to-
envelope validation, and rule-driven domain-error translation (`tether.tools`).
The capability executes live in `tether.bucket_capabilities`, shared with the
REST routes; this module only names each tool's params model and mounts it.

Bucket items are typed, so Add is exposed per item type (`add_movie`,
`add_place`): each tool takes that type's own flat fields, keeping the surface
friendly to a weak model rather than asking it to assemble a polymorphic JSON
payload. The richer/optional fields and a single generic Add live on the REST
surface. Complete, Delete, and Search round out the belt.
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.routing import Route

from tether.bucket_capabilities import (
    BUCKET_ERRORS,
    add_movie,
    add_place,
    complete,
    delete,
    search,
)
from tether.capabilities import bind_params
from tether.tools import ToolEndpoint, ToolRoute


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


def internal_bucket_tool_routes() -> list[Route]:
    """Mount the Bucket item capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Bucket routes (and the Memory tools) so
    they stay absent from the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/add_movie",
            ToolEndpoint(AddMovieParams, bind_params(add_movie), errors=BUCKET_ERRORS),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/add_place",
            ToolEndpoint(AddPlaceParams, bind_params(add_place), errors=BUCKET_ERRORS),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/complete_bucket_item",
            ToolEndpoint(
                CompleteBucketItemParams, bind_params(complete), errors=BUCKET_ERRORS
            ),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/delete_bucket_item",
            ToolEndpoint(
                DeleteBucketItemParams, bind_params(delete), errors=BUCKET_ERRORS
            ),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/search_bucket_items",
            ToolEndpoint(
                SearchBucketItemsParams, bind_params(search), errors=BUCKET_ERRORS
            ),
            methods=["POST"],
        ),
    ]
