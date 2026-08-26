"""Behavior tests for canonical Bucket item mutations."""

from collections.abc import AsyncGenerator

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.bucket_item_store import create_bucket_item_schema, derive_state
from tether.bucket_items import BucketItemConflictError, BucketItemService
from tether.structured_logging import Logger

LOGGER: Logger = structlog.stdlib.get_logger("test.bucket_items")


@fixture
async def bucket_items() -> AsyncGenerator[BucketItemService]:
    """Create the retained mutation service over canonical SQLite."""
    database = await Database.initialize(Config(database=":memory:"))
    await create_bucket_item_schema(database)
    yield BucketItemService(
        database=database,
        tracer=trace.NoOpTracerProvider().get_tracer("test.bucket_items"),
    )
    await database.close()


@test()
async def add_returns_a_duplicate_advisory_without_blocking() -> None:
    """A live duplicate warns while the second canonical item is still inserted."""
    service = await load_fixture(bucket_items())
    _ = await service.add("movie", {"title": "Dune"}, None, logger=LOGGER)

    duplicate = await service.add("movie", {"title": "Dune"}, None, logger=LOGGER)

    assert_eq(duplicate.severity, "warn")
    assert_eq(len(duplicate.duplicates), 1)


@test()
async def complete_retains_the_item_as_terminal_history() -> None:
    """Completion stamps canonical state rather than deleting the row."""
    service = await load_fixture(bucket_items())
    added = await service.add("movie", {"title": "Arrival"}, None, logger=LOGGER)

    completed = await service.complete(added.item, logger=LOGGER)

    assert_eq(derive_state(completed), "completed")
    assert_eq(completed.version, 2)


@test()
async def stale_mutations_conflict() -> None:
    """Optimistic concurrency rejects a second mutation at an old version."""
    service = await load_fixture(bucket_items())
    added = await service.add("movie", {"title": "Arrival"}, None, logger=LOGGER)
    _ = await service.set_intent(added.item, "watch soon", logger=LOGGER)

    with assert_raises(BucketItemConflictError):
        _ = await service.complete(added.item, logger=LOGGER)
