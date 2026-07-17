"""The internal Bucket item tool surface, over the shared response envelope.

These mount alongside the Memory tools under `/internal/tools/*` — the loopback
seam a pi process calls back into — reusing the same auth gate, params-to-
envelope validation, and rule-driven domain-error translation (`tether.tools`).
The capability executes live in `tether.bucket_capabilities`, shared with the
REST routes; this module only names each tool's params model and mounts it.

Bucket items are typed, so Add is exposed per item type (`add_movie`,
`add_place`, `add_book`, `add_travel`): each tool takes that type's own flat
fields, keeping the surface
friendly to a weak model rather than asking it to assemble a polymorphic JSON
payload. The richer/optional fields and a single generic Add live on the REST
surface. Complete, Delete, and Search round out the belt.
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.routing import Route

from tether.bucket_capabilities import (
    BUCKET_ERRORS,
    add_book,
    add_movie,
    add_place,
    add_travel,
    complete,
    delete,
    search,
    set_bucket_item_intent,
)
from tether.capabilities import bind_params
from tether.tools import ToolSpec


class AddMovieParams(BaseModel):
    """Params for Adding a `movie` Bucket item.

    `intent_context` is optional — add the item now even without one; a reason
    can be attached later with `set_bucket_item_intent`.
    """

    title: str
    intent_context: str | None = None
    year: int | None = None


class AddPlaceParams(BaseModel):
    """Params for Adding a `place` Bucket item.

    `intent_context` is optional — add the item now even without one; a reason
    can be attached later with `set_bucket_item_intent`.
    """

    name: str
    intent_context: str | None = None
    location: str | None = None


class AddBookParams(BaseModel):
    """Params for Adding a `book` Bucket item.

    `intent_context` is optional — add the item now even without one; a reason
    can be attached later with `set_bucket_item_intent`.
    """

    title: str
    intent_context: str | None = None
    author: str | None = None


class AddTravelParams(BaseModel):
    """Params for Adding a `travel` Bucket item.

    `intent_context` is optional — add the item now even without one; a reason
    can be attached later with `set_bucket_item_intent`.
    """

    destination: str
    intent_context: str | None = None
    season: str | None = None


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


class SetBucketItemIntentParams(BaseModel):
    """Params for attaching or replacing a Bucket item's intent context.

    The one way to record a reason after Add — use it once the human supplies
    one for an item that was Added without it.
    """

    bucket_item_id: UUID7
    version: PositiveInt
    intent_context: str


BUCKET_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("add_movie", AddMovieParams, bind_params(add_movie), BUCKET_ERRORS),
    ToolSpec("add_place", AddPlaceParams, bind_params(add_place), BUCKET_ERRORS),
    ToolSpec("add_book", AddBookParams, bind_params(add_book), BUCKET_ERRORS),
    ToolSpec("add_travel", AddTravelParams, bind_params(add_travel), BUCKET_ERRORS),
    ToolSpec(
        "complete_bucket_item",
        CompleteBucketItemParams,
        bind_params(complete),
        BUCKET_ERRORS,
    ),
    ToolSpec(
        "delete_bucket_item",
        DeleteBucketItemParams,
        bind_params(delete),
        BUCKET_ERRORS,
    ),
    ToolSpec(
        "search_bucket_items",
        SearchBucketItemsParams,
        bind_params(search),
        BUCKET_ERRORS,
    ),
    ToolSpec(
        "set_bucket_item_intent",
        SetBucketItemIntentParams,
        bind_params(set_bucket_item_intent),
        BUCKET_ERRORS,
    ),
)
"""The Bucket item capabilities exposed as internal tools, in generated order."""


def internal_bucket_tool_routes() -> list[Route]:
    """Mount the Bucket item capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Bucket routes (and the Memory tools) so
    they stay absent from the public OpenAPI document and generated client.
    """
    return [spec.route() for spec in BUCKET_TOOL_SPECS]
