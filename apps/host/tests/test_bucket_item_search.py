"""Behavior tests for Bucket Item Search over canonical SQLite state."""

from collections.abc import AsyncGenerator

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, CurrentTimestamp, Database, insert, update
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.bucket_item_index import BucketItemCandidate
from tether.bucket_item_search import (
    BucketItemSearchService,
    EmptyBucketSearchQueryError,
)
from tether.bucket_item_store import BucketItem, create_bucket_item_schema
from tether.structured_logging import Logger

LOGGER: Logger = structlog.stdlib.get_logger("test.bucket_item_search")


class CandidateSource:
    """Deterministic candidate source whose ranking is supplied by the test."""

    def __init__(self) -> None:
        self.candidates_to_return: list[BucketItemCandidate] = []

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[BucketItemCandidate]:
        """Return the configured candidates in rank order."""
        _ = query, logger
        return self.candidates_to_return[:limit]


@fixture
async def bucket_item_search() -> AsyncGenerator[
    tuple[Database, BucketItemSearchService, CandidateSource]
]:
    """Build Search over a real canonical store and deterministic candidates."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_bucket_item_schema(database)
    source = CandidateSource()
    service = BucketItemSearchService(
        database=database,
        searcher=source,
        tracer=trace.NoOpTracerProvider().get_tracer("test.bucket_item_search"),
    )
    yield database, service, source
    await database.close()


@test()
async def search_rehydrates_candidates_and_excludes_terminal_items() -> None:
    """Search trusts SQLite lifecycle state rather than stale index candidates."""
    database, service, source = await load_fixture(bucket_item_search())
    async with database.transaction(mode="immediate") as transaction:
        active = await transaction.execute(
            insert(
                BucketItem(
                    data={"title": "Dune"},
                    dedup_key="dune",
                    intent_context="recommended",
                    item_type="movie",
                    title="Dune",
                )
            ).returning()
        )
        completed = await transaction.execute(
            insert(
                BucketItem(
                    data={"title": "Dune Messiah"},
                    dedup_key="dune messiah",
                    intent_context="continue series",
                    item_type="movie",
                    title="Dune Messiah",
                )
            ).returning()
        )
        _ = await transaction.execute(
            update(BucketItem)
            .set(BucketItem.completed_at.to(CurrentTimestamp))
            .where(BucketItem.id.eq(completed.id))
        )
    source.candidates_to_return = [
        BucketItemCandidate(id=completed.id, score=1.0),
        BucketItemCandidate(id=active.id, score=0.5),
    ]

    items = await service.search("Dune", logger=LOGGER)

    assert_eq([item.id for item in items], [active.id])


@test()
async def search_rejects_a_blank_query_before_calling_the_index() -> None:
    """Blank Search input remains an expected domain failure."""
    _, service, _ = await load_fixture(bucket_item_search())

    with assert_raises(EmptyBucketSearchQueryError):
        _ = await service.search("   ", logger=LOGGER)
