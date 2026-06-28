"""The SearchIndex adapter: a hybrid retriever over a derived LanceDB projection.

This module is the *sole* importer of `lancedb`/`pyarrow` in the host. It owns a
single embedded LanceDB table at `<index_dir>/` with three columns — `id`,
`content`, and a fixed-size `vector` — and exposes a small async surface the
reconciler and search path drive:

- `upsert` / `remove` / `rebuild` keep the projection in step with SQLite (which
  remains the canonical store; this index is disposable and rebuildable);
- `search` runs LanceDB's native full-text search over `content` and an exact
  flat-scan cosine search over the caller-supplied query vector, fusing the two
  with Reciprocal Rank Fusion (`RRFReranker`).

Design choices baked in here:

- *Native FTS only, no vector ANN index.* The FTS index is created once on table
  creation; new/edited rows are found immediately via flat-scan of the unindexed
  tail. At Tether's single-user scale exact cosine over a flat scan beats a lossy
  IVF_PQ index, so no vector index is ever built.
- *Domain-shaped boundary.* `SearchDocument` in, `SearchCandidate` out. The
  adapter knows nothing about Memories, tethering, or soft-deletes — the
  `tethered ∧ ¬deleted` invariant is enforced upstream against SQLite.

>>> index = await SearchIndex.open(index_dir=Path(".tether/index"), vector_dim=384)
>>> await index.upsert([SearchDocument(id=memory_id, content=text, vector=vec)])
>>> hits = await index.search(text="dentist", vector=query_vec, limit=10)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import pyarrow as pa
from lancedb import connect_async
from lancedb.index import FTS
from lancedb.rerankers import RRFReranker

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from lancedb.db import AsyncConnection
    from lancedb.table import AsyncTable

_TABLE = "memories"
"""The single table name inside the index dataset."""

_ID_COLUMN = "id"
_CONTENT_COLUMN = "content"
_VECTOR_COLUMN = "vector"
_SCORE_COLUMN = "_relevance_score"
"""Column LanceDB attaches to reranked hybrid results (the RRF score)."""


class VectorDimMismatchError(Exception):
    """Raised when an existing index's vector width disagrees with the request.

    The adapter never silently reshapes an index; resolving a dimension change
    (a different embedding model) is the reconciler's job via drop-and-rebuild."""


@dataclass(frozen=True, slots=True)
class SearchDocument:
    """A unit to (re)index: an id, its searchable text, and its embedding."""

    id: UUID
    content: str
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    """A hybrid-search hit: a document id and its fused relevance score."""

    id: UUID
    score: float


def _schema(vector_dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field(_ID_COLUMN, pa.string()),
            pa.field(_CONTENT_COLUMN, pa.string()),
            pa.field(_VECTOR_COLUMN, pa.list_(pa.float32(), vector_dim)),
        ]
    )


class SearchIndex:
    """Async hybrid retriever over an embedded LanceDB table."""

    def __init__(
        self,
        *,
        connection: AsyncConnection,
        table: AsyncTable,
        vector_dim: int,
    ) -> None:
        self._connection: AsyncConnection = connection
        self._table: AsyncTable = table
        self._vector_dim: int = vector_dim

    @property
    def vector_dim(self) -> int:
        """Width of the vectors this index stores; fixes its schema."""
        return self._vector_dim

    @classmethod
    async def open(cls, *, index_dir: Path, vector_dim: int) -> SearchIndex:
        """Open the index at `index_dir`, creating the table if it is absent.

        Idempotent: an existing dataset is reused as-is. If its vector width
        disagrees with `vector_dim`, raises `VectorDimMismatchError` rather than
        corrupting the projection. `connect_async` creates `index_dir` (and any
        missing parents) on first connect."""
        connection = await connect_async(str(index_dir))
        # list_tables() returns a ListTablesResponse, not a list — read .tables.
        existing_tables = (await connection.list_tables()).tables
        if _TABLE in existing_tables:
            table = await connection.open_table(_TABLE)
            await cls._verify_dimension(table, vector_dim)
        else:
            table = await cls._create_table(connection, vector_dim)
        return cls(connection=connection, table=table, vector_dim=vector_dim)

    @staticmethod
    async def _create_table(connection: AsyncConnection, vector_dim: int) -> AsyncTable:
        table = await connection.create_table(_TABLE, schema=_schema(vector_dim))
        # FTS index built once on the empty table; later rows are flat-scanned.
        await table.create_index(_CONTENT_COLUMN, config=FTS())
        return table

    @staticmethod
    async def _verify_dimension(table: AsyncTable, vector_dim: int) -> None:
        schema = await table.schema()
        field_type = schema.field(_VECTOR_COLUMN).type
        if not pa.types.is_fixed_size_list(field_type):  # pragma: no cover - defensive
            message = f"index column {_VECTOR_COLUMN!r} is not a fixed-size vector"
            raise VectorDimMismatchError(message)
        existing = field_type.list_size
        if existing != vector_dim:
            message = (
                f"index vector width {existing} does not match requested {vector_dim}"
            )
            raise VectorDimMismatchError(message)

    async def upsert(self, documents: Sequence[SearchDocument]) -> None:
        """Insert or replace documents by id (content + vector)."""
        rows = [
            {
                _ID_COLUMN: str(document.id),
                _CONTENT_COLUMN: document.content,
                _VECTOR_COLUMN: list(document.vector),
            }
            for document in documents
        ]
        if not rows:
            return
        _ = await (
            self._table.merge_insert(_ID_COLUMN)
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(rows)
        )

    async def remove(self, ids: Sequence[UUID]) -> None:
        """Delete documents by id; ids absent from the index are ignored."""
        if not ids:
            return
        # UUIDs are hex+dashes, so direct interpolation is injection-safe.
        quoted = ", ".join(f"'{identifier}'" for identifier in ids)
        await self._table.delete(f"{_ID_COLUMN} IN ({quoted})")

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[SearchCandidate]:
        """Hybrid search: native FTS on `text` + cosine on `vector`, fused by RRF."""
        rows = await (
            self._table.query()
            .nearest_to(list(vector))
            .nearest_to_text(text)
            .rerank(RRFReranker())
            .limit(limit)
            .to_list()
        )
        return [
            SearchCandidate(
                id=UUID(str(row[_ID_COLUMN])),
                score=float(row[_SCORE_COLUMN]),
            )
            for row in rows
        ]

    async def rebuild(self, documents: Sequence[SearchDocument]) -> None:
        """Drop the table and reindex `documents` from scratch.

        Used when the embedding model changes: vectors from different models are
        incomparable, so the whole projection is rebuilt from SQLite."""
        await self._connection.drop_table(_TABLE)
        self._table = await self._create_table(self._connection, self._vector_dim)
        await self.upsert(documents)

    async def count(self) -> int:
        """Number of documents currently indexed."""
        return await self._table.count_rows()

    async def list_ids(self) -> set[UUID]:
        """Every document id currently in the index.

        The reconciler diffs this against SQLite's `tethered ∧ ¬deleted` set to
        drop orphans (rows whose Memory was deleted or never un-tethered) left
        behind by a missed event — the correctness backstop for the latency
        path."""
        rows = await self._table.query().select([_ID_COLUMN]).to_list()
        return {UUID(str(row[_ID_COLUMN])) for row in rows}

    async def optimize(self) -> None:
        """Run LanceDB's background hygiene (compaction, index maintenance)."""
        _ = await self._table.optimize()
