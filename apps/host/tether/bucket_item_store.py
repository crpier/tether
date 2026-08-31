"""Canonical Bucket Item persistence model and schema migrations."""

from __future__ import annotations

from typing import ClassVar, Literal, TypedDict
from uuid import uuid7

from pydantic import UUID7, Json, PositiveInt
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.bucket_item_model import (
    BookData,
    ItemType,
    JsonValue,
    MovieData,
    PlaceData,
    PurchaseData,
    TravelData,
)

type BucketItemState = Literal["active", "completed", "deleted"]
"""A Bucket item's lifecycle state, derived from its terminal timestamps."""


class BucketItemProvenance(TypedDict):
    kind: Literal["manual"]


class BucketItem[S = Pending](Model[S, "BucketItem[Fetched]"]):
    id: sqlite.GenCol[UUID7] = Text(  # ty: ignore[invalid-assignment]
        primary_key=True,
        default_factory=uuid7,
    )
    item_type: sqlite.Col[ItemType] = Text()
    """The kind of Bucket item; determines its payload fields."""
    title: sqlite.Col[str] = Text()
    """Human-facing display text; the searchable projection of the payload."""
    dedup_key: sqlite.Col[str] = Text()
    """Normalised identity used to find duplicates across all states."""
    data: sqlite.Col[Json[dict[str, JsonValue]]] = Text()
    """The item-type's payload fields, as JSON."""
    intent_context: sqlite.Col[str] = Text()
    """Why the human saved this, if given. Optional at Add (stored as `""` when
    omitted); may be attached or replaced later via `set_intent`."""
    provenance: sqlite.Col[Json[BucketItemProvenance]] = Text(
        default_factory=lambda: BucketItemProvenance(kind="manual"),
    )
    """The objective origin of the Added item."""
    version: sqlite.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    completed_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    deleted_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(item_type, dedup_key)]


def derive_state(item: BucketItem[Fetched]) -> BucketItemState:
    """Derive a Bucket item's lifecycle state from its terminal timestamps.

    Completion and deletion are mutually exclusive terminal transitions, so a
    stamped `deleted_at` or `completed_at` names the terminal state and an
    item with neither is still active.
    """
    if item.deleted_at is not None:
        return "deleted"
    if item.completed_at is not None:
        return "completed"
    return "active"


def _optional_index_text(value: str | int | None) -> list[str]:
    """Project an optional typed payload field into searchable text."""
    return [] if value is None else [str(value)]


def bucket_item_index_text(item: BucketItem[Fetched]) -> str:
    """The searchable projection of a Bucket item: title + type-relevant text.

    Mirrors `_describe_item`'s derivation of `title`/`dedup_key` from the
    already-validated payload, but composes the fuller text a hybrid search
    index should match against — the title plus whichever secondary
    item-type field carries additional identifying text (an author, a
    location, a season), not the raw JSON payload."""
    parts = [item.title]
    if item.item_type == "purchase":
        purchase = PurchaseData.model_validate(item.data)
        parts.extend(_optional_index_text(purchase.store))
        parts.extend(purchase.decision_factors)
        return "\n".join(parts)
    match item.item_type:
        case "movie":
            movie = MovieData.model_validate(item.data)
            parts.extend(_optional_index_text(movie.year))
        case "place":
            place = PlaceData.model_validate(item.data)
            parts.extend(_optional_index_text(place.location))
        case "book":
            book = BookData.model_validate(item.data)
            parts.extend(_optional_index_text(book.author))
        case "travel":
            travel = TravelData.model_validate(item.data)
            parts.extend(_optional_index_text(travel.season))
    return "\n".join(parts)


async def create_bucket_item_schema(database: Database) -> None:
    """Create the Bucket item table and its index on an initialized database.

    Applied as its own migrations after the Memory schema's, mirroring the
    Memory spine's `create_memory_schema`. The table carries a `(item_type,
    dedup_key)` index, so scaffolding emits two statements (table, then index);
    a snekql migration body runs exactly one statement, so each becomes its own
    ordered migration. The caller owns `Database.initialize` and hands the live
    database here before serving requests.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_bucket_item_schema(database)
    """
    migrations = {
        f"002_{label}": sql for label, sql in scaffold_sqlite_statements([BucketItem])
    }
    await database.migrate(migrations)
