"""Behavior tests for the Bucket item service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent — the primary testing seam: call a capability and
assert on observable behavior (DB rows, the dedup advisory), never on internal
structure.

The service under test is `tether.bucket_items.BucketItemService`:

    add(item_type, data, intent_context) -> AddOutcome
    complete(item)                       -> BucketItem
    delete(item)                         -> BucketItem
    search(query)                        -> list[BucketItem]
    browse_by_state(state)               -> list[BucketItem]
"""

import asyncio
from collections.abc import Mapping

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Config, Database, Fetched, delete, select
from snektest import (
    AsyncFixture,
    assert_eq,
    assert_in,
    assert_is_not_none,
    assert_not_in,
    assert_raises,
    load_fixture,
    test,
)

from tether.bucket_items import (
    AddOutcome,
    BucketItem,
    BucketItemConflictError,
    BucketItemNotFoundError,
    BucketItemService,
    BucketItemState,
    EmptyBucketSearchQueryError,
    EmptyIntentContextError,
    InvalidItemDataError,
    ItemType,
    create_bucket_item_schema,
    derive_state,
)
from tether.logging import Logger


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.bucket_item_service")


class LoggedBucketItemService:
    """Test adapter that supplies the mandatory service logger."""

    def __init__(self, service: BucketItemService, *, logger: Logger) -> None:
        self.service: BucketItemService = service
        self.logger: Logger = logger

    @property
    def database(self) -> Database:
        """Expose the wrapped database for DB-observable assertions."""
        return self.service.database

    async def add(
        self,
        item_type: ItemType,
        data: Mapping[str, object],
        intent_context: str = "saved on a whim",
    ) -> AddOutcome:
        """Add through the wrapped service with logging context."""
        return await self.service.add(
            item_type, data, intent_context, logger=self.logger
        )

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
    ) -> list[BucketItem[Fetched]]:
        """Search through the wrapped service with logging context."""
        return await self.service.search(query, limit=limit, logger=self.logger)

    async def browse_by_state(
        self, state: BucketItemState
    ) -> list[BucketItem[Fetched]]:
        """Browse through the wrapped service with logging context."""
        return await self.service.browse_by_state(state, logger=self.logger)

    async def complete(self, item: BucketItem[Fetched]) -> BucketItem[Fetched]:
        """Complete through the wrapped service with logging context."""
        return await self.service.complete(item, logger=self.logger)

    async def delete(self, item: BucketItem[Fetched]) -> BucketItem[Fetched]:
        """Delete through the wrapped service with logging context."""
        return await self.service.delete(item, logger=self.logger)


async def bucket_item_service() -> AsyncFixture[LoggedBucketItemService]:
    """A fresh, isolated Tether database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_bucket_item_schema(db)
    yield LoggedBucketItemService(
        BucketItemService(database=db, tracer=noop_tracer()),
        logger=structlog.stdlib.get_logger("test.bucket_item_service"),
    )
    await db.close()


async def add_item(
    service: LoggedBucketItemService,
    item_type: ItemType = "movie",
    data: Mapping[str, object] | None = None,
    intent_context: str = "saved on a whim",
) -> BucketItem[Fetched]:
    """Add a Bucket item and return the created item (dropping the advisory)."""
    payload = data if data is not None else {"title": "Dune"}
    outcome = await service.add(item_type, payload, intent_context)
    return outcome.item


async def fetch_item_row(
    service: LoggedBucketItemService, item: BucketItem[Fetched]
) -> BucketItem[Fetched] | None:
    """Fetch a Bucket item row directly for DB-observable assertions."""
    async with service.database.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(BucketItem).where(BucketItem.id.eq(item.id))
        )


async def hard_delete_item_row(
    service: LoggedBucketItemService, item: BucketItem[Fetched]
) -> None:
    """Physically remove a row to simulate a missing observed item."""
    async with service.database.transaction() as tx:
        _ = await tx.execute(delete(BucketItem).where(BucketItem.id.eq(item.id)))


# --- Add: typed items, intent context, provenance ---


@test()
async def add_lands_active() -> None:
    """An Added Bucket item starts active."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service)

    assert_eq(derive_state(item), "active")


@test()
async def add_records_item_type() -> None:
    """An Added item carries exactly the item type it was Added under."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service, "place", {"name": "Lisbon"})

    assert_eq(item.item_type, "place")


@test()
async def add_stores_the_typed_payload() -> None:
    """A movie's payload fields round-trip through the JSON data column."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service, "movie", {"title": "Dune", "year": 2021})

    assert_eq(item.data, {"title": "Dune", "year": 2021})


@test()
async def add_stores_a_contrasting_typed_payload() -> None:
    """A place carries its own differently-shaped fields."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service, "place", {"name": "Lisbon", "location": "Portugal"})

    assert_eq(item.data, {"name": "Lisbon", "location": "Portugal"})


@test()
async def add_records_intent_context() -> None:
    """An Added item records why it was saved."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service, intent_context="a podcast recommended it")

    assert_eq(item.intent_context, "a podcast recommended it")


@test()
async def add_trims_intent_context() -> None:
    """Intent context is stored without surrounding whitespace."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service, intent_context="  a friend raved about it  ")

    assert_eq(item.intent_context, "a friend raved about it")


@test()
async def add_rejects_blank_intent_context() -> None:
    """Intent context is required: an empty reason is no reason."""
    service = await load_fixture(bucket_item_service())

    with assert_raises(EmptyIntentContextError):
        _ = await add_item(service, intent_context="   ")


@test()
async def add_records_manual_provenance() -> None:
    """A human Add only ever produces manual provenance."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service)

    assert_eq(item.provenance, {"kind": "manual"})


@test()
async def add_rejects_an_invalid_payload() -> None:
    """A payload missing its item type's required field is a domain error."""
    service = await load_fixture(bucket_item_service())

    with assert_raises(InvalidItemDataError):
        _ = await add_item(service, "movie", {"year": 2021})


@test()
async def add_starts_at_version_one() -> None:
    """Optimistic concurrency starts from the first observed revision."""
    service = await load_fixture(bucket_item_service())

    item = await add_item(service)

    assert_eq(item.version, 1)


# --- Intent context immutability ---


@test()
async def completing_an_item_preserves_intent_context() -> None:
    """Intent context is immutable: a terminal transition leaves it untouched."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, intent_context="a friend recommended it")

    _ = await service.complete(item)

    row = await fetch_item_row(service, item)
    assert row is not None, "completed item is retained as history"
    assert_eq(row.intent_context, "a friend recommended it")


@test()
async def deleting_an_item_preserves_intent_context() -> None:
    """Deleting leaves the immutable intent context recorded on the row."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, intent_context="relates to my interest in X")

    _ = await service.delete(item)

    row = await fetch_item_row(service, item)
    assert row is not None, "deleted item is retained as history"
    assert_eq(row.intent_context, "relates to my interest in X")


# --- Complete / delete: terminal states retained as history ---


@test()
async def completing_an_item_moves_it_to_completed() -> None:
    """Completing moves an active item to the terminal completed state."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    completed = await service.complete(item)

    assert_eq(derive_state(completed), "completed")


@test()
async def completing_an_item_stamps_completed_at() -> None:
    """Completing records when the item was finished."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    completed = await service.complete(item)

    _ = assert_is_not_none(completed.completed_at)


@test()
async def completing_an_item_retains_the_row() -> None:
    """A completed item is retained permanently as history."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    _ = await service.complete(item)

    row = await fetch_item_row(service, item)
    assert row is not None, "completed item must remain in the DB"


@test()
async def deleting_an_item_moves_it_to_deleted() -> None:
    """Deleting moves an active item to the terminal deleted state."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    deleted = await service.delete(item)

    assert_eq(derive_state(deleted), "deleted")


@test()
async def deleting_an_item_retains_the_row() -> None:
    """A deleted item is retained permanently as history (not hard-deleted)."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    _ = await service.delete(item)

    row = await fetch_item_row(service, item)
    assert row is not None, "deleted item must remain in the DB"


@test()
async def completing_bumps_version() -> None:
    """Completing consumes one observed revision and returns the next."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    completed = await service.complete(item)

    assert_eq(completed.version, item.version + 1)


@test()
async def completing_an_already_completed_item_conflicts() -> None:
    """A terminal item cannot be completed again."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)
    _ = await service.complete(item)

    with assert_raises(BucketItemConflictError):
        _ = await service.complete(item)


@test()
async def deleting_a_completed_item_conflicts() -> None:
    """Terminal states are final: a completed item cannot then be deleted."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)
    completed = await service.complete(item)

    with assert_raises(BucketItemConflictError):
        _ = await service.delete(completed)


@test()
async def completing_a_missing_item_raises() -> None:
    """Operating on an absent item is a well-formed not-found error."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)
    await hard_delete_item_row(service, item)

    with assert_raises(BucketItemNotFoundError):
        _ = await service.complete(item)


# --- Dedup: warns on active, informs on terminal, never blocks ---


@test()
async def adding_a_unique_item_reports_no_duplicates() -> None:
    """A first-of-its-kind Add has severity none."""
    service = await load_fixture(bucket_item_service())

    outcome = await service.add("movie", {"title": "Dune"}, "recommended")

    assert_eq(outcome.severity, "none")


@test()
async def re_adding_an_active_item_warns() -> None:
    """Re-adding something already active warns of the duplicate."""
    service = await load_fixture(bucket_item_service())
    _ = await add_item(service, "movie", {"title": "Dune"})

    outcome = await service.add("movie", {"title": "Dune"}, "saw it again")

    assert_eq(outcome.severity, "warn")


@test()
async def re_adding_an_active_item_still_creates_it() -> None:
    """Dedup never hard-blocks: the duplicate is still Added."""
    service = await load_fixture(bucket_item_service())
    first = await add_item(service, "movie", {"title": "Dune"})

    outcome = await service.add("movie", {"title": "Dune"}, "saw it again")

    assert_eq(derive_state(outcome.item), "active")
    assert outcome.item.id != first.id, "a distinct row is created"


@test()
async def re_adding_an_active_item_surfaces_the_duplicate() -> None:
    """The advisory carries the pre-existing active duplicate."""
    service = await load_fixture(bucket_item_service())
    first = await add_item(service, "movie", {"title": "Dune"})

    outcome = await service.add("movie", {"title": "Dune"}, "saw it again")

    assert_in(first.id, [duplicate.id for duplicate in outcome.duplicates])


@test()
async def re_adding_a_completed_item_informs_but_allows() -> None:
    """Re-adding something already completed informs ('you watched this')."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, "movie", {"title": "Dune"})
    _ = await service.complete(item)

    outcome = await service.add("movie", {"title": "Dune"}, "want to rewatch")

    assert_eq(outcome.severity, "inform")
    assert_eq(derive_state(outcome.item), "active")


@test()
async def re_adding_a_deleted_item_informs_but_allows() -> None:
    """Re-adding something previously deleted informs ('you once dismissed this')."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, "movie", {"title": "Dune"})
    _ = await service.delete(item)

    outcome = await service.add("movie", {"title": "Dune"}, "reconsidering")

    assert_eq(outcome.severity, "inform")
    assert_eq(derive_state(outcome.item), "active")


@test()
async def dedup_warns_when_any_duplicate_is_active() -> None:
    """An active duplicate dominates terminal ones: severity is warn."""
    service = await load_fixture(bucket_item_service())
    first = await add_item(service, "movie", {"title": "Dune"})
    _ = await service.complete(first)
    _ = await add_item(service, "movie", {"title": "Dune"})

    outcome = await service.add("movie", {"title": "Dune"}, "again")

    assert_eq(outcome.severity, "warn")


@test()
async def dedup_is_case_and_whitespace_insensitive() -> None:
    """Dedup compares identity, not presentation."""
    service = await load_fixture(bucket_item_service())
    _ = await add_item(service, "movie", {"title": "The Matrix"})

    outcome = await service.add("movie", {"title": "the   matrix"}, "again")

    assert_eq(outcome.severity, "warn")


@test()
async def dedup_does_not_span_item_types() -> None:
    """A movie and a place with the same text are not duplicates."""
    service = await load_fixture(bucket_item_service())
    _ = await add_item(service, "movie", {"title": "Lisbon"})

    outcome = await service.add("place", {"name": "Lisbon"}, "visit someday")

    assert_eq(outcome.severity, "none")


@test()
async def dedup_distinguishes_movies_by_year() -> None:
    """Same title, different year is a different intention."""
    service = await load_fixture(bucket_item_service())
    _ = await add_item(service, "movie", {"title": "Dune", "year": 1984})

    outcome = await service.add("movie", {"title": "Dune", "year": 2021}, "the remake")

    assert_eq(outcome.severity, "none")


# --- Search: matching active items ---


@test()
async def search_returns_matching_active_items() -> None:
    """Keyword Search returns active items whose title matches."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, "movie", {"title": "Blade Runner"})

    found = [hit.id for hit in await service.search("Blade")]

    assert_in(item.id, found)


@test()
async def search_excludes_completed_items() -> None:
    """A completed item drops out of the active Search."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, "movie", {"title": "Blade Runner"})
    _ = await service.complete(item)

    found = [hit.id for hit in await service.search("Blade")]

    assert_not_in(item.id, found)


@test()
async def search_excludes_deleted_items() -> None:
    """A deleted item drops out of the active Search."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service, "movie", {"title": "Blade Runner"})
    _ = await service.delete(item)

    found = [hit.id for hit in await service.search("Blade")]

    assert_not_in(item.id, found)


@test()
async def search_ands_terms_together() -> None:
    """Keyword Search includes items containing every query term."""
    service = await load_fixture(bucket_item_service())
    matching = await add_item(service, "movie", {"title": "Blade Runner 2049"})
    non_matching = await add_item(service, "movie", {"title": "Blade of the Immortal"})

    found = [hit.id for hit in await service.search("Blade Runner")]

    assert_in(matching.id, found)
    assert_not_in(non_matching.id, found)


@test()
async def search_requires_a_non_empty_query() -> None:
    """Keyword Search rejects blank queries instead of listing everything."""
    service = await load_fixture(bucket_item_service())

    with assert_raises(EmptyBucketSearchQueryError):
        _ = await service.search("   ")


@test()
async def search_orders_matches_newest_first() -> None:
    """Keyword Search is unranked, so recency orders equal LIKE matches."""
    service = await load_fixture(bucket_item_service())
    older = await add_item(service, "movie", {"title": "needle older"})
    await asyncio.sleep(0.01)
    newer = await add_item(service, "movie", {"title": "needle newer"})

    found = [hit.id for hit in await service.search("needle")]

    assert_eq(found, [newer.id, older.id])


@test()
async def search_caps_results_at_the_given_limit() -> None:
    """Keyword Search returns at most `limit` matches."""
    service = await load_fixture(bucket_item_service())
    for index in range(3):
        _ = await add_item(service, "movie", {"title": f"needle {index}"})

    found = await service.search("needle", limit=2)

    assert_eq(len(found), 2)


# --- Browse by state: active list + retained history ---


@test()
async def browse_active_returns_active_items() -> None:
    """The active list surfaces items still to be acted on."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)

    found = [hit.id for hit in await service.browse_by_state("active")]

    assert_in(item.id, found)


@test()
async def browse_active_excludes_terminal_items() -> None:
    """The active list never shows completed or deleted items."""
    service = await load_fixture(bucket_item_service())
    completed = await add_item(service, "movie", {"title": "Done"})
    _ = await service.complete(completed)
    deleted = await add_item(service, "movie", {"title": "Gone"})
    _ = await service.delete(deleted)

    found = [hit.id for hit in await service.browse_by_state("active")]

    assert_not_in(completed.id, found)
    assert_not_in(deleted.id, found)


@test()
async def browse_completed_returns_completed_history() -> None:
    """Completed history is browsable for dedup and review."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)
    _ = await service.complete(item)

    found = [hit.id for hit in await service.browse_by_state("completed")]

    assert_in(item.id, found)


@test()
async def browse_deleted_returns_deleted_history() -> None:
    """Deleted history is retained and browsable."""
    service = await load_fixture(bucket_item_service())
    item = await add_item(service)
    _ = await service.delete(item)

    found = [hit.id for hit in await service.browse_by_state("deleted")]

    assert_in(item.id, found)
