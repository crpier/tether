"""The TranscriptIndex adapter: the chunk-shaped projection of `HybridLanceTable`.

A sibling of `SearchIndex` (Memories) over the same generic hybrid retriever in
`tether.hybrid_lance_table`, which owns the LanceDB mechanics (native FTS +
flat-scan cosine fused by RRF, merge-insert upserts, the self-healing
`optimize`). This module fixes the transcript column set — `id` (a chunk uuid),
a `video_id` payload column, `content`, and the vector — and translates at the
boundary: `ChunkDocument` in, `ChunkCandidate` out. A hit returns the parent
`video_id` plus the chunk text as a snippet, so the search path can dedupe
chunks to videos and show why each matched without re-fetching the transcript.

>>> index = await TranscriptIndex.open(index_dir=Path(".tether/yt-index"), vector_dim=384)
>>> await index.upsert([ChunkDocument(id=chunk_id, video_id="abc", content=text, vector=vec)])
>>> hits = await index.search(text="android signing", vector=query_vec, limit=10)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from tether.hybrid_lance_table import HybridLanceTable, TableDocument

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tether.logging import Logger

_TABLE = "transcript_chunks"
"""The single table name inside the transcript index dataset."""

_VIDEO_COLUMN = "video_id"
"""Payload column carrying each chunk's parent video id."""

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


def _to_table_document(document: ChunkDocument) -> TableDocument:
    """Project a transcript chunk onto the generic table row shape."""
    return TableDocument(
        id=document.id,
        content=document.content,
        vector=document.vector,
        payload={_VIDEO_COLUMN: document.video_id},
    )


class TranscriptIndex:
    """Async hybrid retriever over the embedded LanceDB table of chunks."""

    def __init__(self, *, table: HybridLanceTable) -> None:
        self._table: HybridLanceTable = table

    @property
    def vector_dim(self) -> int:
        """Width of the vectors this index stores; fixes its schema."""
        return self._table.vector_dim

    @classmethod
    async def open(cls, *, index_dir: Path, vector_dim: int) -> TranscriptIndex:
        """Open the index at `index_dir`, creating the table if it is absent.

        Idempotent: an existing dataset is reused as-is. If its vector width
        disagrees with `vector_dim`, raises `VectorDimMismatchError` rather than
        corrupting the projection."""
        return cls(
            table=await HybridLanceTable.open(
                index_dir=index_dir,
                table_name=_TABLE,
                vector_dim=vector_dim,
                payload_columns=(_VIDEO_COLUMN,),
            )
        )

    async def upsert(self, documents: Sequence[ChunkDocument]) -> None:
        """Insert or replace chunks by id (video_id + content + vector)."""
        await self._table.upsert(
            [_to_table_document(document) for document in documents]
        )

    async def remove(self, ids: Sequence[UUID]) -> None:
        """Delete chunks by id; ids absent from the index are ignored."""
        await self._table.remove(ids)

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[ChunkCandidate]:
        """Hybrid search: native FTS on `text` + cosine on `vector`, fused by RRF."""
        hits = await self._table.search(text=text, vector=vector, limit=limit)
        return [
            ChunkCandidate(
                chunk_id=hit.id,
                video_id=hit.payload[_VIDEO_COLUMN],
                snippet=hit.content,
                score=hit.score,
            )
            for hit in hits
        ]

    async def rebuild(self, documents: Sequence[ChunkDocument]) -> None:
        """Drop the table and reindex `documents` from scratch.

        Used when the embedding model changes (incomparable vector spaces) or to
        repopulate a wiped index; chunks are re-derived and re-embedded from the
        canonical transcripts upstream."""
        await self._table.rebuild(
            [_to_table_document(document) for document in documents]
        )

    async def count(self) -> int:
        """Number of chunks currently indexed."""
        return await self._table.count()

    async def list_ids(self) -> set[UUID]:
        """Every chunk id currently in the index, for orphan diffing."""
        return await self._table.list_ids()

    async def optimize(self, *, logger: Logger) -> None:
        """Run LanceDB's background hygiene (compaction, index maintenance).

        Delegates to the self-healing `HybridLanceTable.optimize`, which
        salvages the known lance compaction corruption in place instead of
        wedging the reconcile loop that drives this on every tick."""
        await self._table.optimize(logger=logger)
