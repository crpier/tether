"""The Bucket item domain's capability descriptor.

The pieces the REST routes (`tether.bucket_routes`) and the internal tools
(`tether.bucket_tools`) both need live here once: the Read models, the
detached-reference builder, the domain→code map (`BUCKET_ERRORS`), and one
execute function per capability — the service call plus its Read-model
rendering. Add is one shared execute; the per-type tool spellings
(`add_movie`, `add_place`, `add_book`, `add_travel`) project their flat
fields onto it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request

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
from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger

BUCKET_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule(
        (BucketItemNotFoundError,), "not_found", 404, detail="bucket item not found"
    ),
    ErrorRule((BucketItemConflictError,), "conflict", 409),
    ErrorRule((EmptyBucketSearchQueryError,), "invalid_input", 400),
    ErrorRule((InvalidItemDataError, EmptyIntentContextError), "invalid_input", 422),
)
"""The Bucket item domain→code map both surfaces translate failures through."""


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
    enough; the other columns are required placeholders with no role here.
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


def _single(item: BucketItem[Fetched]) -> CapabilityOutcome:
    """Render a single-item outcome, surfacing its provenance."""
    return CapabilityOutcome(
        result=BucketItemRead.from_item(item).model_dump(mode="json"),
        provenance=item.provenance,
    )


def _many(items: list[BucketItem[Fetched]]) -> CapabilityOutcome:
    """Render a Bucket item collection; provenance is null for collections."""
    return CapabilityOutcome(
        result=[
            BucketItemRead.from_item(item).model_dump(mode="json") for item in items
        ]
    )


async def add(
    request: Request,
    item_type: ItemType,
    data: dict[str, JsonValue],
    intent_context: str,
) -> CapabilityOutcome:
    """Add a Bucket item; the outcome carries its dedup advisory."""
    outcome = await request.app.state.bucket_item_service.add(
        item_type,
        data,
        intent_context,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=AddBucketItemResponse.from_outcome(outcome).model_dump(mode="json"),
        provenance=outcome.item.provenance,
    )


async def add_movie(
    request: Request, title: str, intent_context: str, year: int | None = None
) -> CapabilityOutcome:
    """Add a `movie` Bucket item from its flat tool fields."""
    data: dict[str, Any] = {"title": title}
    if year is not None:
        data["year"] = year
    return await add(request, "movie", data, intent_context)


async def add_place(
    request: Request, name: str, intent_context: str, location: str | None = None
) -> CapabilityOutcome:
    """Add a `place` Bucket item from its flat tool fields."""
    data: dict[str, Any] = {"name": name}
    if location is not None:
        data["location"] = location
    return await add(request, "place", data, intent_context)


async def add_book(
    request: Request, title: str, intent_context: str, author: str | None = None
) -> CapabilityOutcome:
    """Add a `book` Bucket item from its flat tool fields."""
    data: dict[str, Any] = {"title": title}
    if author is not None:
        data["author"] = author
    return await add(request, "book", data, intent_context)


async def add_travel(
    request: Request, destination: str, intent_context: str, season: str | None = None
) -> CapabilityOutcome:
    """Add a `travel` Bucket item from its flat tool fields."""
    data: dict[str, Any] = {"destination": destination}
    if season is not None:
        data["season"] = season
    return await add(request, "travel", data, intent_context)


async def browse(request: Request, state: BucketItemState) -> CapabilityOutcome:
    """List Bucket items in a lifecycle state (active list / retained history)."""
    items = await request.app.state.bucket_item_service.browse_by_state(
        state,
        logger=get_request_logger(request),
    )
    return _many(items)


async def search(request: Request, q: str, limit: int = 50) -> CapabilityOutcome:
    """Keyword Search over active Bucket items."""
    items = await request.app.state.bucket_item_service.search(
        q,
        limit=limit,
        logger=get_request_logger(request),
    )
    return _many(items)


async def complete(
    request: Request, bucket_item_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Complete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.complete(
        _bucket_item_reference(bucket_item_id, version),
        logger=get_request_logger(request),
    )
    return _single(item)


async def delete(
    request: Request, bucket_item_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Delete a Bucket item, moving it to terminal history."""
    item = await request.app.state.bucket_item_service.delete(
        _bucket_item_reference(bucket_item_id, version),
        logger=get_request_logger(request),
    )
    return _single(item)
