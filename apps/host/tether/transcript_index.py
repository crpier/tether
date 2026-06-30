"""The TranscriptIndex adapter: a hybrid retriever over transcript chunks.

A sibling of `SearchIndex` (Memories), this owns a second embedded LanceDB table
at `<index_dir>/` shaped for transcript chunks. Each row carries four columns —
`id` (a chunk uuid), `video_id` (the parent video), `content` (the chunk text),
and a fixed-size `vector`. A hit returns the parent `video_id` plus the chunk
text as a snippet, so the search path can dedupe chunks to videos and show why
each matched without re-fetching the transcript.

It is a deliberate twin rather than a generalization of `SearchIndex`: the two
stay domain-shaped (Memory ids in/out there, video ids + snippets here) so
neither carries the other's concerns. Design choices match `SearchIndex` — native
FTS only (no vector ANN index; exact flat-scan cosine wins at single-user scale),
RRF hybrid fusion, and a disposable projection rebuilt from canonical SQLite.

>>> index = await TranscriptIndex.open(index_dir=Path(".tether/yt-index"), vector_dim=384)
>>> await index.upsert([ChunkDocument(id=chunk_id, video_id="abc", content=text, vector=vec)])
>>> hits = await index.search(text="android signing", vector=query_vec, limit=10)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

import pyarrow as pa
from lancedb import connect_async
from lancedb.index import FTS
from lancedb.rerankers import RRFReranker

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from lancedb.db import AsyncConnection
    from lancedb.table import AsyncTable

_TABLE = "transcript_chunks"
"""The single table name inside the transcript index dataset."""

_ID_COLUMN = "id"
_VIDEO_COLUMN = "video_id"
_CONTENT_COLUMN = "content"
_VECTOR_COLUMN = "vector"
_SCORE_COLUMN = "_relevance_score"
"""Column LanceDB attaches to reranked hybrid results (the RRF score)."""

_CHUNK_NAMESPACE = UUID("6f3c0a1e-2b7d-5e84-9a1f-0c4d8e2b6f10")
"""Fixed namespace for deterministic chunk ids (uuid5)."""


def chunk_id(
    *, model: str, vector_dim: int, video_id: str, index: int, content: str
) -> UUID:
    """Derive a stable chunk id from its identity *and* its embedding context.

    The id folds in the active model + vector width, so a model swap changes
    every chunk id: the reconciler then re-embeds the whole corpus under the new
    model (the new ids are absent from the index) and drops the old ids as
    orphans — a rebuild driven entirely by the `list_ids()` diff, with no
    separate model marker. Content is included too, so editing a transcript
    re-embeds only the chunks whose text actually changed."""
    key = f"{model}\x00{vector_dim}\x00{video_id}\x00{index}\x00{content}"
    return uuid5(_CHUNK_NAMESPACE, key)


class VectorDimMismatchError(Exception):
    """Raised when an existing index's vector width disagrees with the request.

    The adapter never silently reshapes an index; resolving a dimension change
    (a different embedding model) is the reconciler's job via drop-and-rebuild."""


@dataclass(frozen=True, slots=True)
class ChunkDocument:
    """A transcript chunk to (re)index: id, parent video, text, embedding."""

    id: UUID
    video_id: str
    content: str
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    """A hybrid-search hit: the chunk, its parent video, snippet, and score."""

    chunk_id: UUID
    video_id: str
    snippet: str
    score: float


def _schema(vector_dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field(_ID_COLUMN, pa.string()),
            pa.field(_VIDEO_COLUMN, pa.string()),
            pa.field(_CONTENT_COLUMN, pa.string()),
            pa.field(_VECTOR_COLUMN, pa.list_(pa.float32(), vector_dim)),
        ]
    )


class TranscriptIndex:
    """Async hybrid retriever over an embedded LanceDB table of chunks."""

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
    async def open(cls, *, index_dir: Path, vector_dim: int) -> TranscriptIndex:
        """Open the index at `index_dir`, creating the table if it is absent.

        Idempotent: an existing dataset is reused as-is. If its vector width
        disagrees with `vector_dim`, raises `VectorDimMismatchError` rather than
        corrupting the projection."""
        connection = await connect_async(str(index_dir))
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
        # `with_position` stores token offsets so phrase queries (quoted terms,
        # which the model emits freely) resolve instead of raising "position is
        # not found but required for phrase queries".
        await table.create_index(
            _CONTENT_COLUMN,
            # lancedb ships no py.typed, so pyright can't see FTS's dataclass
            # fields and reads its init as no-arg; the kwarg is valid at runtime.
            config=FTS(with_position=True),  # pyright: ignore[reportCallIssue]
        )
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

    async def upsert(self, documents: Sequence[ChunkDocument]) -> None:
        """Insert or replace chunks by id (video_id + content + vector)."""
        rows = [
            {
                _ID_COLUMN: str(document.id),
                _VIDEO_COLUMN: document.video_id,
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
        """Delete chunks by id; ids absent from the index are ignored."""
        if not ids:
            return
        # UUIDs are hex+dashes, so direct interpolation is injection-safe.
        quoted = ", ".join(f"'{identifier}'" for identifier in ids)
        await self._table.delete(f"{_ID_COLUMN} IN ({quoted})")

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[ChunkCandidate]:
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
            ChunkCandidate(
                chunk_id=UUID(str(row[_ID_COLUMN])),
                video_id=str(row[_VIDEO_COLUMN]),
                snippet=str(row[_CONTENT_COLUMN]),
                score=float(row[_SCORE_COLUMN]),
            )
            for row in rows
        ]

    async def rebuild(self, documents: Sequence[ChunkDocument]) -> None:
        """Drop the table and reindex `documents` from scratch.

        Used when the embedding model changes (incomparable vector spaces) or to
        repopulate a wiped index; chunks are re-derived and re-embedded from the
        canonical transcripts upstream."""
        await self._connection.drop_table(_TABLE)
        self._table = await self._create_table(self._connection, self._vector_dim)
        await self.upsert(documents)

    async def count(self) -> int:
        """Number of chunks currently indexed."""
        return await self._table.count_rows()

    async def list_ids(self) -> set[UUID]:
        """Every chunk id currently in the index, for orphan diffing."""
        rows = await self._table.query().select([_ID_COLUMN]).to_list()
        return {UUID(str(row[_ID_COLUMN])) for row in rows}

    async def optimize(self) -> None:
        """Run LanceDB's background hygiene (compaction, index maintenance)."""
        _ = await self._table.optimize()
