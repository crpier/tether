"""Behavior tests for deterministic Bucket item search."""

from collections.abc import AsyncGenerator

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.bucket_item_search import (
    BucketItemSearchService,
    EmptyBucketSearchQueryError,
)
from tether.bucket_item_store import create_bucket_item_schema
from tether.bucket_items import BucketItemService
from tether.structured_logging import Logger

LOGGER: Logger = structlog.stdlib.get_logger("test.bucket_item_search")


@fixture
async def search_services() -> AsyncGenerator[
    tuple[BucketItemService, BucketItemSearchService]
]:
    """Create retained Bucket services over one canonical in-memory database."""
    database = await Database.initialize(Config(database=":memory:"))
    await create_bucket_item_schema(database)
    tracer = trace.NoOpTracerProvider().get_tracer("test.bucket_item_search")
    yield (
        BucketItemService(database=database, tracer=tracer),
        BucketItemSearchService(database=database, tracer=tracer),
    )
    await database.close()


@test()
async def search_matches_all_terms_case_insensitively() -> None:
    """Search projects typed payload fields and intent into normalized text."""
    bucket_items, search = await load_fixture(search_services())
    _ = await bucket_items.add(
        "book",
        {"title": "The Dispossessed", "author": "Ursula Le Guin"},
        "Science fiction reading list",
        logger=LOGGER,
    )
    _ = await bucket_items.add(
        "book",
        {"title": "Dune", "author": "Frank Herbert"},
        "Science fiction reading list",
        logger=LOGGER,
    )

    matches = await search.search("URSULA fiction", logger=LOGGER)

    assert_eq([item.title for item in matches], ["The Dispossessed"])


@test()
async def search_excludes_terminal_items() -> None:
    """Completed items remain canonical history but disappear from active search."""
    bucket_items, search = await load_fixture(search_services())
    added = await bucket_items.add("movie", {"title": "Arrival"}, None, logger=LOGGER)
    _ = await bucket_items.complete(added.item, logger=LOGGER)

    matches = await search.search("Arrival", logger=LOGGER)

    assert_eq(matches, [])


@test()
async def search_caps_results_at_fifty() -> None:
    """The service enforces its output cap even below the schema boundary."""
    bucket_items, search = await load_fixture(search_services())
    for index in range(51):
        _ = await bucket_items.add(
            "movie", {"title": f"Shared title {index}"}, None, logger=LOGGER
        )

    matches = await search.search("shared", limit=51, logger=LOGGER)

    assert_eq(len(matches), 50)


@test()
async def search_rejects_a_blank_query() -> None:
    """Whitespace cannot become an accidental browse-all operation."""
    _, search = await load_fixture(search_services())

    with assert_raises(EmptyBucketSearchQueryError):
        _ = await search.search("   ", logger=LOGGER)
