"""Search-metadata: the singleton marker that governs index rebuilds.

`search_meta` records the *active embedding model* and its vector dimension,
plus an *index schema version*. It is the trigger the reconciler reads to decide
when the derived LanceDB projection must be dropped and rebuilt: a model change
re-embeds the whole corpus, because vector spaces from different models can't be
compared. The table is a singleton — one row, replaced in place.

>>> service = SearchMetaService(database=database)
>>> await service.set(model="BAAI/bge-small-en-v1.5", vector_dim=384, logger=logger)
>>> marker = await service.fetch(logger=logger)
>>> marker.embedding_model
'BAAI/bge-small-en-v1.5'
"""

from __future__ import annotations

from pydantic import PositiveInt
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    DoUpdate,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    scaffold,
    select,
)

from tether.structured_logging import Logger

_SINGLETON_ID = 1
"""The only valid primary key: search_meta holds exactly one row."""


class SearchMeta[S = Pending](Model[S, "SearchMeta[Fetched]"]):
    id: sqlite.Col[int] = Integer(primary_key=True, default=_SINGLETON_ID)
    """Fixed singleton key; the table never holds more than one row."""
    embedding_model: sqlite.Col[str] = Text()
    """Identifier of the model that produced the corpus's current vectors."""
    vector_dim: sqlite.Col[PositiveInt] = Integer()
    """Dimension of those vectors; the LanceDB schema is built from it."""
    index_schema_version: sqlite.Col[PositiveInt] = Integer(default=1)
    """Bumped when the index layout (not the model) changes incompatibly."""
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class SearchMetaService:
    """Read/replace the singleton search-metadata marker over a snekql database."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def fetch(self, *, logger: Logger) -> SearchMeta[Fetched] | None:
        """Return the active marker, or `None` if no embedding has run yet."""
        logger.debug("Reading search metadata marker")
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(SearchMeta).where(SearchMeta.id.eq(_SINGLETON_ID))
            )

    async def set(
        self,
        *,
        model: str,
        vector_dim: PositiveInt,
        logger: Logger,
    ) -> SearchMeta[Fetched]:
        """Record the active model + dimension, replacing any prior marker."""
        logger.info(
            "Recording search metadata marker",
            embedding_model=model,
            vector_dim=vector_dim,
        )

        async with self.database.transaction(mode="immediate") as tx:
            return await tx.execute(
                insert(
                    SearchMeta(
                        id=_SINGLETON_ID,
                        embedding_model=model,
                        vector_dim=vector_dim,
                    )
                )
                .on_conflict(
                    SearchMeta.id,
                    action=DoUpdate(
                        SearchMeta.embedding_model.to_inserted(),
                        SearchMeta.vector_dim.to_inserted(),
                        SearchMeta.updated_at.to(CurrentTimestamp),
                    ),
                )
                .returning()
            )


async def create_search_meta_schema(database: Database) -> None:
    """Create the search_meta table on an already-initialized database.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_search_meta_schema(database)
    """
    await database.migrate({"008_search_meta": scaffold([SearchMeta])})
