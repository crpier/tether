"""Behaviour tests for the host-computed Triage report.

These drive `tether.triage.TriageService` directly against a real (in-memory)
SQLite database. The report is pure host computation — the under-specified
heuristic, duplicate clustering, and staleness with decayed intent context — so
every assertion is on that behaviour over seeded items, never on model prose
(there is none). Items are seeded through `BucketItemService` exactly as the
production producers would, and staleness is asserted against an injected clock.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched
from snektest import (
    assert_eq,
    assert_in,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.bucket_items import (
    BucketItem,
    BucketItemService,
    ItemType,
    create_bucket_item_schema,
)
from tether.logging import Logger
from tether.triage import (
    STALE_AFTER_DAYS,
    DecayedIntentContext,
    TriageReport,
    TriageService,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.triage_service")


class TriageHarness:
    """Seed Bucket items through the real service, then compute a report."""

    def __init__(
        self,
        bucket_service: BucketItemService,
        triage_service: TriageService,
        *,
        logger: Logger,
    ) -> None:
        self.bucket_service: BucketItemService = bucket_service
        self.triage_service: TriageService = triage_service
        self.logger: Logger = logger

    async def add(
        self,
        item_type: ItemType,
        data: dict[str, Any],
        intent_context: str = "a friend recommended it",
    ) -> BucketItem[Fetched]:
        """Add one active Bucket item and return it."""
        outcome = await self.bucket_service.add(
            item_type, data, intent_context, logger=self.logger
        )
        return outcome.item

    async def complete(self, item: BucketItem[Fetched]) -> BucketItem[Fetched]:
        """Move an item to terminal `completed`."""
        return await self.bucket_service.complete(item, logger=self.logger)

    async def report(self, *, now: datetime | None = None) -> TriageReport:
        """Compute the Triage report, optionally against an injected clock."""
        return await self.triage_service.triage_report(now=now, logger=self.logger)


@fixture
async def triage_harness() -> AsyncGenerator[TriageHarness]:
    """A fresh isolated database with a Bucket service and a Triage service."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_bucket_item_schema(db)
    yield TriageHarness(
        BucketItemService(database=db, tracer=noop_tracer()),
        TriageService(database=db),
        logger=structlog.stdlib.get_logger("test.triage_service"),
    )
    await db.close()


def _active_ids(report: TriageReport) -> list[UUID7]:
    """The ids of the active items the report lists."""
    return [item.id for item in report.active]


def _stale_context(report: TriageReport, item_id: UUID7) -> DecayedIntentContext:
    """Return the decayed intent context the report attached to a stale item."""
    by_id = {stale.bucket_item_id: stale for stale in report.stale}
    assert item_id in by_id, "item not surfaced as stale"
    return by_id[item_id].intent_context


def _far_future(item: BucketItem[Fetched], *, extra_days: int = 0) -> datetime:
    """A clock far enough past an item's creation to clear the staleness bar."""
    return item.created_at + timedelta(days=STALE_AFTER_DAYS + extra_days)


# --- Active membership ---


@test()
async def active_lists_active_items() -> None:
    """The report's active list surfaces items still on the live backlog."""
    harness = await load_fixture(triage_harness())
    item = await harness.add("movie", {"title": "Dune", "year": 2021})

    report = await harness.report()

    assert_in(item.id, _active_ids(report))


@test()
async def active_excludes_terminal_items() -> None:
    """A completed item has left the active backlog Triage reports over."""
    harness = await load_fixture(triage_harness())
    item = await harness.add("movie", {"title": "Dune", "year": 2021})
    _ = await harness.complete(item)

    report = await harness.report()

    assert_not_in(item.id, _active_ids(report))


# --- Under-specified ---


@test()
async def under_specified_flags_movie_without_year() -> None:
    """A movie with no release year cannot be acted on — flag it."""
    harness = await load_fixture(triage_harness())
    vague = await harness.add("movie", {"title": "Dune"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_in(vague.id, flagged)


@test()
async def under_specified_flags_place_without_location() -> None:
    """A place with no location is too vague to visit — flag it."""
    harness = await load_fixture(triage_harness())
    vague = await harness.add("place", {"name": "that cafe"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_in(vague.id, flagged)


@test()
async def under_specified_flags_book_without_author() -> None:
    """A book with no author is too ambiguous to track down — flag it."""
    harness = await load_fixture(triage_harness())
    vague = await harness.add("book", {"title": "Dune"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_in(vague.id, flagged)


@test()
async def under_specified_flags_travel_without_season() -> None:
    """A trip with no season can never leave the someday pile — flag it."""
    harness = await load_fixture(triage_harness())
    vague = await harness.add("travel", {"destination": "Japan"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_in(vague.id, flagged)


@test()
async def fully_specified_book_is_not_flagged() -> None:
    """A book carrying its author clears the bar."""
    harness = await load_fixture(triage_harness())
    precise = await harness.add("book", {"title": "Dune", "author": "Frank Herbert"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_not_in(precise.id, flagged)


@test()
async def fully_specified_travel_is_not_flagged() -> None:
    """A trip carrying its season clears the bar."""
    harness = await load_fixture(triage_harness())
    precise = await harness.add("travel", {"destination": "Japan", "season": "spring"})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_not_in(precise.id, flagged)


@test()
async def fully_specified_item_is_not_flagged() -> None:
    """An item carrying its distinguishing field clears the bar."""
    harness = await load_fixture(triage_harness())
    precise = await harness.add("movie", {"title": "Dune", "year": 2021})

    report = await harness.report()

    flagged = {item.bucket_item_id for item in report.under_specified}
    assert_not_in(precise.id, flagged)


# --- Duplicates ---


@test()
async def duplicate_clusters_active_items_sharing_identity() -> None:
    """Two live items competing for the same intention cluster together."""
    harness = await load_fixture(triage_harness())
    first = await harness.add("movie", {"title": "Dune", "year": 2021})
    second = await harness.add("movie", {"title": "dune", "year": 2021})

    report = await harness.report()

    clusters = [set(cluster.bucket_item_ids) for cluster in report.duplicates]
    assert_in({first.id, second.id}, clusters)


@test()
async def duplicates_exclude_distinct_items() -> None:
    """Items with different identities are not a duplicate cluster."""
    harness = await load_fixture(triage_harness())
    dune = await harness.add("movie", {"title": "Dune", "year": 2021})
    _ = await harness.add("movie", {"title": "Arrival", "year": 2016})

    report = await harness.report()

    clustered = [
        cluster for cluster in report.duplicates if dune.id in cluster.bucket_item_ids
    ]
    assert_eq(clustered, [])


@test()
async def duplicates_ignore_terminal_twins() -> None:
    """A completed twin is settled history, not a live duplicate."""
    harness = await load_fixture(triage_harness())
    done = await harness.add("movie", {"title": "Dune", "year": 2021})
    _ = await harness.complete(done)
    active = await harness.add("movie", {"title": "Dune", "year": 2021})

    report = await harness.report()

    clustered = [
        cluster for cluster in report.duplicates if active.id in cluster.bucket_item_ids
    ]
    assert_eq(clustered, [])


# --- Stale + decayed intent context ---


@test()
async def stale_items_surface_with_decayed_intent_context() -> None:
    """An item past the staleness cut-off surfaces with its immutable why."""
    harness = await load_fixture(triage_harness())
    old = await harness.add(
        "movie", {"title": "Dune", "year": 2021}, "a podcast recommended it"
    )

    report = await harness.report(now=_far_future(old, extra_days=60))

    context = _stale_context(report, old.id)
    assert_eq(context.intent_context, "a podcast recommended it")
    assert_true(context.age_days >= STALE_AFTER_DAYS)
    assert_true(0.0 < context.decay < 1.0)


@test()
async def fresh_items_are_not_stale() -> None:
    """An item created just now is not surfaced as stale."""
    harness = await load_fixture(triage_harness())
    fresh = await harness.add("movie", {"title": "Dune", "year": 2021})

    report = await harness.report()

    assert_not_in(fresh.id, [stale.bucket_item_id for stale in report.stale])


@test()
async def decay_grows_with_age() -> None:
    """An older intention reads as more decayed than a younger one."""
    harness = await load_fixture(triage_harness())
    item = await harness.add("movie", {"title": "Dune", "year": 2021})

    younger = await harness.report(now=_far_future(item, extra_days=0))
    older = await harness.report(now=_far_future(item, extra_days=360))

    assert_true(
        _stale_context(older, item.id).decay > _stale_context(younger, item.id).decay
    )


# --- No new stored state ---


@test()
async def triage_produces_no_new_stored_state() -> None:
    """Running Triage leaves the items it reports over untouched."""
    harness = await load_fixture(triage_harness())
    item = await harness.add("movie", {"title": "Dune"})

    _ = await harness.report()
    again = await harness.report()

    surviving = {read.id: read.version for read in again.active}
    assert_eq(surviving, {item.id: item.version})
