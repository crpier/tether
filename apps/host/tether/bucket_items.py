"""Bucket item service layer: Add, Complete, Delete, Search, with dedup.

A Bucket item is a typed intention to act (a movie to watch, a place to visit).
It is Added `active` under exactly one item type, each type carrying its own
payload fields, and records — immutably — the human's intent context (*why* it
was saved) plus its provenance. It moves to a terminal state when Completed or
Deleted; terminal rows are retained permanently as history so dedup can reason
across the whole past.

Dedup spans every state and **informs but never hard-blocks**: Adding always
succeeds and returns an advisory — `warn` when an active duplicate already
exists, `inform` when the only duplicates are already completed or deleted.

>>> service = BucketItemService(database=database, tracer=tracer)
>>> outcome = await service.add(
...     "movie", {"title": "Dune"}, "a friend recommended it", logger=logger
... )
>>> outcome.severity
'none'
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal, TypedDict
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel, Json, PositiveInt, ValidationError
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.logging import Logger

type BucketItemState = Literal["active", "completed", "deleted"]
"""A Bucket item's lifecycle state, derived from its terminal timestamps."""

type ItemType = Literal["movie", "place"]
"""The kind of a Bucket item; determines which payload fields it carries."""

type DedupSeverity = Literal["none", "warn", "inform"]
"""How loudly dedup speaks about pre-existing duplicates of an Added item."""

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class BucketItemProvenance(TypedDict):
    kind: Literal["manual"]


class BucketItemNotFoundError(Exception):
    """Raised when an operation targets a Bucket item that does not exist."""


class BucketItemConflictError(Exception):
    """Raised when a live Bucket item cannot accept the requested operation.

    A domain-state or stale-version conflict, not absence: e.g. completing an
    item that is already terminal, or acting on a stale observed version.
    """


class EmptyBucketSearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class EmptyIntentContextError(Exception):
    """Raised when intent context is blank after trimming whitespace."""


class InvalidItemDataError(Exception):
    """Raised when an item-type payload fails its type's validation."""


class MovieData(BaseModel):
    """The payload fields a `movie` Bucket item carries."""

    title: str
    year: int | None = None


class PlaceData(BaseModel):
    """The payload fields a `place` Bucket item carries."""

    name: str
    location: str | None = None


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


def _normalise_key(text: str) -> str:
    """Collapse a payload string to its dedup-comparison form.

    Dedup is about identity, not presentation, so case and surrounding/internal
    whitespace are noise: "The  Matrix" and "the matrix" are the same intention.
    """
    return " ".join(text.lower().split())


def _normalise_intent(intent_context: str) -> str:
    """Trim intent context, rejecting a blank reason.

    Intent context answers "why did I save this?" months later; an empty reason
    is not a reason. It is required at Add and never edited afterward.
    """
    normalised = intent_context.strip()
    if not normalised:
        msg = "intent context must not be blank"
        raise EmptyIntentContextError(msg)
    return normalised


@dataclass(frozen=True, slots=True)
class _ItemDescription:
    """The derived facts an Add needs from a validated item-type payload."""

    data: dict[str, JsonValue]
    dedup_key: str
    title: str


def _describe_item(item_type: ItemType, data: Mapping[str, object]) -> _ItemDescription:
    """Validate a raw payload for its item type and derive its stored facts.

    Each item type owns how it builds its dedup key (the identity dedup compares)
    and its title (the human-facing, searchable projection of the payload). The
    raw payload is validated through the type's Pydantic model so a malformed
    payload is a well-formed domain error, never a corrupt row.
    """
    try:
        match item_type:
            case "movie":
                movie = MovieData.model_validate(data)
                dedup_key = _normalise_key(movie.title)
                if movie.year is not None:
                    dedup_key = f"{dedup_key}|{movie.year}"
                return _ItemDescription(
                    data=movie.model_dump(mode="json"),
                    dedup_key=dedup_key,
                    title=movie.title,
                )
            case "place":
                place = PlaceData.model_validate(data)
                dedup_key = _normalise_key(place.name)
                if place.location is not None:
                    dedup_key = f"{dedup_key}|{_normalise_key(place.location)}"
                return _ItemDescription(
                    data=place.model_dump(mode="json"),
                    dedup_key=dedup_key,
                    title=place.name,
                )
    except ValidationError as error:
        message = (
            f"invalid {item_type} payload: {error.errors(include_url=False)[0]['msg']}"
        )
        raise InvalidItemDataError(message) from error


class BucketItem[S = Pending](Model[S, "BucketItem[Fetched]"]):
    id: BucketItem.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    item_type: BucketItem.Col[ItemType] = Text()
    """The kind of Bucket item; determines its payload fields."""
    title: BucketItem.Col[str] = Text()
    """Human-facing display text; the searchable projection of the payload."""
    dedup_key: BucketItem.Col[str] = Text()
    """Normalised identity used to find duplicates across all states."""
    data: BucketItem.Col[Json[dict[str, JsonValue]]] = Text()
    """The item-type's payload fields, as JSON."""
    intent_context: BucketItem.Col[str] = Text()
    """Why the human saved this. Set at Add, immutable thereafter."""
    provenance: BucketItem.Col[Json[BucketItemProvenance]] = Text(
        default_factory=lambda: BucketItemProvenance(kind="manual"),
    )
    """The objective origin of the Added item."""
    version: BucketItem.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: BucketItem.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: BucketItem.GenCol[datetime] = Text(default=CurrentTimestamp)
    completed_at: BucketItem.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )
    deleted_at: BucketItem.Col[datetime | None] = Text(
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


@dataclass(frozen=True, slots=True)
class AddOutcome:
    """The result of Adding a Bucket item: the new item plus a dedup advisory.

    `duplicates` are the pre-existing items (any state) sharing this item's
    identity, newest-first; `severity` summarises them — `warn` if any is still
    active, `inform` if all are terminal, `none` if there were no duplicates.
    Adding never blocks, so an item is always created regardless of severity.
    """

    item: BucketItem[Fetched]
    duplicates: list[BucketItem[Fetched]]
    severity: DedupSeverity


def _dedup_severity(duplicates: list[BucketItem[Fetched]]) -> DedupSeverity:
    """Summarise pre-existing duplicates into an advisory severity."""
    if not duplicates:
        return "none"
    if any(derive_state(duplicate) == "active" for duplicate in duplicates):
        return "warn"
    return "inform"


class BucketItemService:
    """Capability surface for Bucket items, over a snekql database.

    Each mutation owns its own transaction (one mutation, one commit) and
    returns the resulting item so the REST and tool layers can echo it.
    """

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
    ) -> None:
        self.database: Database = database
        self.tracer: Tracer = tracer

    async def add(
        self,
        item_type: ItemType,
        data: Mapping[str, object],
        intent_context: str,
        *,
        logger: Logger,
    ) -> AddOutcome:
        """Add an active Bucket item and report any pre-existing duplicates.

        Records the immutable intent context and manual provenance. Dedup is
        computed inside the same transaction that inserts, against every state,
        and only ever informs — the item is created regardless of severity.
        """
        normalised_intent = _normalise_intent(intent_context)
        description = _describe_item(item_type, data)
        with self.tracer.start_as_current_span(
            "BucketItemService.add",
            attributes={"bucket_item.item_type": item_type},
        ) as span:
            _debug(logger, "Adding Bucket item", item_type=item_type)
            async with self.database.transaction() as tx:
                duplicates = await tx.fetch_all(
                    select(BucketItem)
                    .where(
                        BucketItem.item_type.eq(item_type)
                        & BucketItem.dedup_key.eq(description.dedup_key)
                    )
                    .order_by(BucketItem.created_at.desc())
                )
                item = await tx.execute(
                    insert(
                        BucketItem(
                            item_type=item_type,
                            title=description.title,
                            dedup_key=description.dedup_key,
                            data=description.data,
                            intent_context=normalised_intent,
                        )
                    ).returning()
                )
            severity = _dedup_severity(duplicates)
            span.set_attribute("bucket_item.id", str(item.id))
            span.set_attribute("bucket_item.dedup_severity", severity)
            span.set_attribute("bucket_item.duplicate_count", len(duplicates))
            _info(
                logger,
                "Bucket item added",
                bucket_item_id=str(item.id),
                item_type=item_type,
                dedup_severity=severity,
                duplicate_count=len(duplicates),
            )
            return AddOutcome(item=item, duplicates=duplicates, severity=severity)

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """Keyword Search over active Bucket items.

        Placeholder matcher mirroring the Memory spine: the query is split into
        whitespace terms, each matched case-insensitively with `LIKE` against the
        title and AND-ed; results are active-only, newest-first, unranked, capped
        at `limit` (default 50)."""
        terms = query.split()
        if not terms:
            msg = "keyword Search requires a non-empty query"
            raise EmptyBucketSearchQueryError(msg)
        _debug(logger, "Searching Bucket items", terms_count=len(terms), limit=limit)
        active_matches = select(BucketItem).where(
            BucketItem.completed_at.is_null() & BucketItem.deleted_at.is_null()
        )
        for term in terms:
            active_matches = active_matches.where(BucketItem.title.like(f"%{term}%"))
        async with self.database.transaction() as tx:
            items = await tx.fetch_all(
                active_matches.order_by(BucketItem.created_at.desc()).limit(limit)
            )
        _debug(
            logger,
            "Bucket item Search completed",
            terms_count=len(terms),
            limit=limit,
            result_count=len(items),
        )
        return items

    async def browse_by_state(
        self,
        state: BucketItemState,
        *,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """List Bucket items in a given lifecycle state, newest-first.

        `active` is the live list; `completed` and `deleted` are the retained
        history dedup reasons over. Each is ordered by the timestamp that defines
        the state (creation for active, the terminal stamp otherwise), newest
        first."""
        _debug(logger, "Browsing Bucket items by state", state=state)
        match state:
            case "active":
                browse = (
                    select(BucketItem)
                    .where(
                        BucketItem.completed_at.is_null()
                        & BucketItem.deleted_at.is_null()
                    )
                    .order_by(BucketItem.created_at.desc())
                )
            case "completed":
                browse = (
                    select(BucketItem)
                    .where(
                        BucketItem.completed_at.is_not_null()
                        & BucketItem.deleted_at.is_null()
                    )
                    .order_by(BucketItem.completed_at.desc())
                )
            case "deleted":
                browse = (
                    select(BucketItem)
                    .where(BucketItem.deleted_at.is_not_null())
                    .order_by(BucketItem.deleted_at.desc())
                )
        async with self.database.transaction() as tx:
            items = await tx.fetch_all(browse)
        _debug(
            logger,
            "Bucket item browse completed",
            state=state,
            result_count=len(items),
        )
        return items

    async def complete(
        self,
        item: BucketItem[Fetched],
        *,
        logger: Logger,
    ) -> BucketItem[Fetched]:
        """Move an active Bucket item to the terminal `completed` state.

        The row is retained as history. Completing a non-active item conflicts;
        an absent item raises; a stale observed version conflicts."""
        return await self._terminate(
            item,
            terminal_state="completed",
            logger=logger,
        )

    async def delete(
        self,
        item: BucketItem[Fetched],
        *,
        logger: Logger,
    ) -> BucketItem[Fetched]:
        """Move an active Bucket item to the terminal `deleted` state.

        Deletion is terminal-but-retained: the row stays in the DB as history so
        dedup can still surface it. Deleting a non-active item conflicts; an
        absent item raises; a stale observed version conflicts."""
        return await self._terminate(
            item,
            terminal_state="deleted",
            logger=logger,
        )

    async def _terminate(
        self,
        item: BucketItem[Fetched],
        *,
        terminal_state: Literal["completed", "deleted"],
        logger: Logger,
    ) -> BucketItem[Fetched]:
        """Stamp one terminal timestamp on an active item, sharing the guard.

        Complete and Delete are the same transition onto different columns: move
        an `active` item — at the observed version — to a terminal state, leaving
        the row in place. Centralising it keeps "only an active item terminates"
        identical across both.
        """
        _debug(
            logger,
            "Terminating Bucket item",
            bucket_item_id=str(item.id),
            terminal_state=terminal_state,
            observed_version=item.version,
        )
        terminate = (
            update(BucketItem)
            .set(BucketItem.updated_at.to(CurrentTimestamp))
            .set(BucketItem.version.to(item.version + 1))
        )
        if terminal_state == "completed":
            terminate = terminate.set(BucketItem.completed_at.to(CurrentTimestamp))
        else:
            terminate = terminate.set(BucketItem.deleted_at.to(CurrentTimestamp))
        async with self.database.transaction() as tx:
            matched_rows = await tx.execute(
                terminate.where(BucketItem.id.eq(item.id))
                .where(BucketItem.completed_at.is_null())
                .where(BucketItem.deleted_at.is_null())
                .where(BucketItem.version.eq(item.version))
            )
            fresh_item = await self._fetch(tx, item.id)
            if matched_rows == 0:
                if derive_state(fresh_item) != "active":
                    _debug(
                        logger,
                        "Bucket item terminate conflict",
                        bucket_item_id=str(item.id),
                        reason="already_terminal",
                        current_state=derive_state(fresh_item),
                    )
                    msg = f"Bucket item {item.id} is already {derive_state(fresh_item)}"
                    raise BucketItemConflictError(msg)
                _debug(
                    logger,
                    "Bucket item terminate conflict",
                    bucket_item_id=str(item.id),
                    reason="stale_version",
                    observed_version=item.version,
                    current_version=fresh_item.version,
                )
                msg = (
                    f"Tried to update Bucket item {item.id} with version "
                    f"{item.version} but it had version {fresh_item.version}"
                )
                raise BucketItemConflictError(msg)
        _info(
            logger,
            "Bucket item terminated",
            bucket_item_id=str(fresh_item.id),
            terminal_state=terminal_state,
            previous_version=item.version,
            version=fresh_item.version,
        )
        return fresh_item

    async def _fetch(
        self, tx: Transaction, bucket_item_id: UUID7
    ) -> BucketItem[Fetched]:
        """Fetch a Bucket item by id or raise.

        Unlike the Memory spine, a terminal Bucket item is not hidden: it is
        retained history, a legitimate target for inspection and the reason a
        terminate conflicts rather than 404s. So this fetches in any state and
        only raises when the row is genuinely absent.
        """
        item = await tx.fetch_one_or_none(
            select(BucketItem).where(BucketItem.id.eq(bucket_item_id))
        )
        if item is None:
            raise BucketItemNotFoundError(bucket_item_id)
        return item


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
