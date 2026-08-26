"""Bucket Item mutation service: Add, Complete, Delete, and curation.

A Bucket item is a typed intention to act (a movie to watch, a place to visit).
It is Added `active` under exactly one item type, each type carrying its own
payload fields, and records the human's intent context (*why* it was saved,
optional at Add — attach or replace it later with `set_intent`) plus its
provenance. It moves to a terminal state when Completed or Deleted; terminal
rows are retained permanently as history so dedup can reason across the whole
past.

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
from typing import Literal

from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.bucket_item_model import (
    DedupSeverity,
    ItemType,
    PurchaseData,
    PurchaseDecision,
    describe_item,
    normalise_intent,
)
from tether.bucket_item_store import BucketItem, derive_state
from tether.structured_logging import Logger


class BucketItemNotFoundError(Exception):
    """Raised when an operation targets a Bucket item that does not exist."""


class BucketItemConflictError(Exception):
    """Raised when a live Bucket item cannot accept the requested operation.

    A domain-state or stale-version conflict, not absence: e.g. completing an
    item that is already terminal, or acting on a stale observed version.
    """


class NotPurchaseItemError(Exception):
    """Raised when a purchase-only operation targets another item type."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


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
    returns the resulting item so the tool layer can echo it.
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
        intent_context: str | None,
        *,
        logger: Logger,
    ) -> AddOutcome:
        """Add an active Bucket item and report any pre-existing duplicates.

        Intent context is optional: a `None` reason (nothing supplied) stores
        as `""` rather than blocking the Add — the item lands immediately, and
        a reason can be attached afterward through `set_intent`. A reason that
        *was* supplied must not be blank whitespace. Dedup is computed inside
        the same transaction that inserts, against every state, and only ever
        informs — the item is created regardless of severity.
        """
        normalised_intent = normalise_intent(intent_context)
        description = describe_item(item_type, data)
        with self.tracer.start_as_current_span(
            "BucketItemService.add",
            attributes={"bucket_item.item_type": item_type},
        ) as span:
            _debug(logger, "Adding Bucket item", item_type=item_type)

            async def _add(
                tx: Transaction,
            ) -> tuple[BucketItem[Fetched], list[BucketItem[Fetched]]]:
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
                return item, duplicates

            async with self.database.transaction(mode="immediate") as tx:
                item, duplicates = await _add(tx)
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

        async def _terminate_tx(tx: Transaction) -> BucketItem[Fetched]:
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
            return fresh_item

        async with self.database.transaction(mode="immediate") as tx:
            fresh_item = await _terminate_tx(tx)
        _info(
            logger,
            "Bucket item terminated",
            bucket_item_id=str(fresh_item.id),
            terminal_state=terminal_state,
            previous_version=item.version,
            version=fresh_item.version,
        )
        return fresh_item

    async def set_purchase_decision(
        self,
        item: BucketItem[Fetched],
        decision: PurchaseDecision,
        *,
        logger: Logger,
    ) -> BucketItem[Fetched]:
        """Record the human's current decision on an active purchase.

        Decisions remain editable because price and store context can change.
        The mutation is optimistic-concurrency checked and rejects non-purchase
        items rather than smuggling purchase behavior into every item type.
        """
        _debug(
            logger,
            "Setting purchase decision",
            bucket_item_id=str(item.id),
            decision=decision,
            observed_version=item.version,
        )

        async def _set_decision_tx(tx: Transaction) -> BucketItem[Fetched]:
            current = await self._fetch(tx, item.id)
            if current.item_type != "purchase":
                raise NotPurchaseItemError(item.id)
            if current.version != item.version:
                msg = (
                    f"Tried to update Bucket item {item.id} with version "
                    f"{item.version} but it had version {current.version}"
                )
                raise BucketItemConflictError(msg)
            purchase = PurchaseData.model_validate(current.data)
            next_data = purchase.model_copy(update={"decision": decision}).model_dump(
                mode="json"
            )
            _ = await tx.execute(
                update(BucketItem)
                .set(BucketItem.data.to(next_data))
                .set(BucketItem.updated_at.to(CurrentTimestamp))
                .set(BucketItem.version.to(item.version + 1))
                .where(BucketItem.id.eq(item.id))
                .where(BucketItem.version.eq(item.version))
            )
            return await self._fetch(tx, item.id)

        async with self.database.transaction(mode="immediate") as tx:
            fresh_item = await _set_decision_tx(tx)
        _info(
            logger,
            "Purchase decision set",
            bucket_item_id=str(fresh_item.id),
            decision=decision,
            version=fresh_item.version,
        )
        return fresh_item

    async def set_intent(
        self,
        item: BucketItem[Fetched],
        intent_context: str,
        *,
        logger: Logger,
    ) -> BucketItem[Fetched]:
        """Attach or replace intent context on an existing Bucket item.

        The one place intent context changes after Add — the common case is an
        item Added without a reason, with the human supplying one moments
        later. Optimistic-concurrency checked like Complete/Delete (the caller
        sends its observed `version`); works in any lifecycle state, since a
        reason can surface after an item has already been completed or
        deleted.
        """
        normalised_intent = normalise_intent(intent_context)
        _debug(
            logger,
            "Setting Bucket item intent context",
            bucket_item_id=str(item.id),
            observed_version=item.version,
        )
        set_intent = (
            update(BucketItem)
            .set(BucketItem.updated_at.to(CurrentTimestamp))
            .set(BucketItem.version.to(item.version + 1))
            .set(BucketItem.intent_context.to(normalised_intent))
        )

        async def _set_intent_tx(tx: Transaction) -> BucketItem[Fetched]:
            matched_rows = await tx.execute(
                set_intent.where(BucketItem.id.eq(item.id)).where(
                    BucketItem.version.eq(item.version)
                )
            )
            fresh_item = await self._fetch(tx, item.id)
            if matched_rows == 0:
                _debug(
                    logger,
                    "Bucket item set-intent conflict",
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
            return fresh_item

        async with self.database.transaction(mode="immediate") as tx:
            fresh_item = await _set_intent_tx(tx)
        _info(
            logger,
            "Bucket item intent context set",
            bucket_item_id=str(fresh_item.id),
            previous_version=item.version,
            version=fresh_item.version,
        )
        return fresh_item

    async def _fetch(
        self, tx: Transaction, bucket_item_id: UUID7
    ) -> BucketItem[Fetched]:
        """Fetch a Bucket item by id or raise.

        A terminal Bucket item remains retained history and is a legitimate
        mutation target, so this fetches in any state and raises only when the
        row is genuinely absent.
        """
        item = await tx.fetch_one_or_none(
            select(BucketItem).where(BucketItem.id.eq(bucket_item_id))
        )
        if item is None:
            raise BucketItemNotFoundError(bucket_item_id)
        return item
