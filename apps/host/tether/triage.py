"""Triage: the host-computed report over the active Bucket items.

Issue #21 surfaces the problems lurking in the active Bucket list — items that
are **under-specified**, **duplicate**, or **stale** — in one place, so the
backlog stays healthy without manual grooming. Triage is the Bucket-side mirror
of Memory's Review, and the two never share vocabulary (CONTEXT.md): Review is a
trust gate that ends in a human tether; Triage is a pure report that ends in
nothing stored.

Everything mechanical — the under-specified heuristic, duplicate clustering, and
staleness with its decayed intent context — is computed in plain Python so the
load-bearing behaviour is testable; the model only narrates the result. The
report is recomputed from live SQLite on each call (ADR-0006): no new tables, no
persisted flags, **no new stored state** at all, so there is nothing to
invalidate and Triage can run on demand or on a Scheduled trigger identically.

Stale items carry their **decayed intent context**: the immutable *why* the
human saved the item, paired with how far that reason has eroded with age, so a
months-old intention can be judged on whether it still holds.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import UUID7, BaseModel
from snekql.sqlite import Database, Fetched, select

from tether.bucket_capabilities import BucketItemRead
from tether.bucket_items import BucketItem, ItemType
from tether.logging import Logger

STALE_AFTER_DAYS = 180
"""Age past which an untouched active item is stale enough to reconsider.

Six months: long enough that a saved intention has plausibly gone cold, short
enough that the example case (something saved eight months ago) lands well
inside it.
"""

_INTENT_HALF_LIFE_DAYS = 180
"""Age at which a saved reason's freshness has halved.

The decay curve is anchored here rather than at the staleness cut-off so the
two can move independently: staleness decides *whether* to surface an item,
decay describes *how far gone* its intent context is.
"""

_MIN_CLUSTER_SIZE = 2
"""A duplicate cluster is only worth surfacing when two or more items share it."""


def _decay(age_days: int) -> float:
    """Fraction of an intent context's freshness eroded by age, in [0, 1).

    Exponential half-life decay: 0.0 the day it was saved, 0.5 at the half-life,
    asymptotic to (but never reaching) 1.0. Monotonic in age so an older
    intention always reads as more decayed than a younger one.
    """
    return 1.0 - 0.5 ** (age_days / _INTENT_HALF_LIFE_DAYS)


class UnderSpecifiedItem(BaseModel):
    """An active item whose payload lacks the detail to act on it later."""

    bucket_item_id: UUID7
    reason: str


class DuplicateCluster(BaseModel):
    """Two or more active items that share one identity (item type + dedup key)."""

    bucket_item_ids: list[UUID7]


class DecayedIntentContext(BaseModel):
    """A stale item's immutable *why*, paired with how far it has eroded."""

    intent_context: str
    age_days: int
    decay: float


class StaleItem(BaseModel):
    """An active item old enough to reconsider, with its decayed intent context."""

    bucket_item_id: UUID7
    intent_context: DecayedIntentContext


class TriageReport(BaseModel):
    """The full Triage view of the active Bucket list at one point in time."""

    active: list[BucketItemRead]
    under_specified: list[UnderSpecifiedItem]
    duplicates: list[DuplicateCluster]
    stale: list[StaleItem]


def _under_specified_reason(item: BucketItem[Fetched]) -> str | None:
    """Name what an active item is missing, or `None` when it is specified enough.

    Each item type owns its own bar: the distinguishing optional field that turns
    a vague intention into an actionable one (a movie's year, a place's
    location). Mirrors `_describe_item`'s per-type match so the heuristic stays
    beside the payload shapes it judges.
    """
    item_type: ItemType = item.item_type
    match item_type:
        case "movie":
            distinguishing_field, reason = "year", "movie is missing its release year"
        case "place":
            distinguishing_field, reason = "location", "place is missing its location"
        case "book":
            distinguishing_field, reason = "author", "book is missing its author"
        case "travel":
            distinguishing_field, reason = "season", "travel is missing its season"
    if item.data.get(distinguishing_field) is None:
        return reason
    return None


def _duplicate_clusters(active: list[BucketItem[Fetched]]) -> list[DuplicateCluster]:
    """Cluster active items sharing one identity, preserving newest-first order.

    Triage looks only at the *active* list, not dedup's full cross-state history:
    two live items competing for the same intention is the problem to surface;
    a completed twin is settled history, not a backlog wart.
    """
    by_identity: dict[tuple[str, str], list[UUID7]] = {}
    for item in active:
        by_identity.setdefault((item.item_type, item.dedup_key), []).append(item.id)
    return [
        DuplicateCluster(bucket_item_ids=ids)
        for ids in by_identity.values()
        if len(ids) >= _MIN_CLUSTER_SIZE
    ]


def _stale_items(active: list[BucketItem[Fetched]], now: datetime) -> list[StaleItem]:
    """Surface active items older than the staleness cut-off, decay attached."""
    stale: list[StaleItem] = []
    for item in active:
        age_days = (now - item.created_at).days
        if age_days >= STALE_AFTER_DAYS:
            stale.append(
                StaleItem(
                    bucket_item_id=item.id,
                    intent_context=DecayedIntentContext(
                        intent_context=item.intent_context,
                        age_days=age_days,
                        decay=_decay(age_days),
                    ),
                )
            )
    return stale


class TriageService:
    """Read-only capability that derives the Triage report over active items.

    Holds only the database: the report never mutates, so it needs neither the
    event bus nor any other collaborator. Every call recomputes from live SQLite.
    """

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def triage_report(
        self,
        *,
        now: datetime | None = None,
        logger: Logger,
    ) -> TriageReport:
        """Compute under-specified, duplicate, and stale over the live active list.

        `now` is injectable so staleness is testable against a controlled clock;
        production passes none and reads the wall clock. Soft-terminal items
        (completed / deleted) are excluded — Triage is about the live backlog.
        """
        moment = now or datetime.now(UTC)
        logger.debug("Computing triage report")
        async with self.database.transaction() as tx:
            active = await tx.fetch_all(
                select(BucketItem)
                .where(
                    BucketItem.completed_at.is_null() & BucketItem.deleted_at.is_null()
                )
                .order_by(BucketItem.created_at.desc())
            )
        report = TriageReport(
            active=[BucketItemRead.from_item(item) for item in active],
            under_specified=[
                UnderSpecifiedItem(bucket_item_id=item.id, reason=reason)
                for item in active
                if (reason := _under_specified_reason(item)) is not None
            ],
            duplicates=_duplicate_clusters(active),
            stale=_stale_items(active, moment),
        )
        logger.debug(
            "Triage report computed",
            active_count=len(report.active),
            under_specified_count=len(report.under_specified),
            duplicate_cluster_count=len(report.duplicates),
            stale_count=len(report.stale),
        )
        return report
