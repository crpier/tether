"""Behavior tests for the search-metadata table.

`search_meta` is a singleton row recording the *active embedding model* and the
*index schema version*. The reconciler reads it to decide when the LanceDB
projection must be dropped and rebuilt (a model change re-embeds the whole
corpus, since vector spaces can't be mixed). These tests drive the schema +
accessor directly against an in-memory SQLite database — no HTTP, no agent.
"""

from collections.abc import AsyncGenerator

import structlog
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.search_meta import (
    SearchMetaService,
    create_search_meta_schema,
)


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.search_meta")


@fixture
async def search_meta_service() -> AsyncGenerator[SearchMetaService]:
    """A fresh, isolated database with only the search_meta schema applied."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_search_meta_schema(db)
    yield SearchMetaService(database=db)
    await db.close()


@test()
async def an_unwritten_marker_reads_as_none() -> None:
    """Before any embedding runs, there is no active model marker."""
    service = await load_fixture(search_meta_service())

    marker = await service.fetch(logger=_logger())

    assert_is_none(marker)


@test()
async def setting_the_marker_persists_model_and_dimension() -> None:
    """Recording the active model stores its name and vector dimension."""
    service = await load_fixture(search_meta_service())

    await service.set(model="BAAI/bge-small-en-v1.5", vector_dim=384, logger=_logger())

    marker = await service.fetch(logger=_logger())
    assert_is_not_none(marker)
    assert marker is not None
    assert_eq(marker.embedding_model, "BAAI/bge-small-en-v1.5")
    assert_eq(marker.vector_dim, 384)


@test()
async def setting_the_marker_again_overwrites_the_singleton() -> None:
    """A model change replaces the single marker row rather than appending."""
    service = await load_fixture(search_meta_service())
    await service.set(model="BAAI/bge-small-en-v1.5", vector_dim=384, logger=_logger())

    await service.set(model="intfloat/e5-large-v2", vector_dim=1024, logger=_logger())

    marker = await service.fetch(logger=_logger())
    assert_is_not_none(marker)
    assert marker is not None
    assert_eq(marker.embedding_model, "intfloat/e5-large-v2")
    assert_eq(marker.vector_dim, 1024)
