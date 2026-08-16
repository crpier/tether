"""Behavior tests for the trusted Memory Search read path."""

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_raises, test

from tether.memory_search import EmptySearchQueryError, MemorySearchService
from tether.memory_store import create_memory_schema


@test()
async def search_rejects_a_blank_query() -> None:
    """Search rejects input that has no content after trimming."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(database)
    service = MemorySearchService(
        database=database,
        searcher=None,
        tracer=trace.NoOpTracerProvider().get_tracer("test.memory_search"),
    )

    with assert_raises(EmptySearchQueryError):
        _ = await service.search(
            "  ", logger=structlog.stdlib.get_logger("test.memory_search")
        )

    await database.close()
