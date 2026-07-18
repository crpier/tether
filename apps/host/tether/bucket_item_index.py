"""The BucketItemIndex adapter: the Bucket-item-shaped projection of
`HybridLanceTable`.

A sibling of `SearchIndex` (Memories) and `TranscriptIndex` (transcript chunks)
over the same generic hybrid retriever in `tether.hybrid_lance_table`, which
owns the LanceDB mechanics (native FTS + flat-scan cosine fused by RRF,
merge-insert upserts, the self-healing `optimize`). This module fixes the
Bucket-item column set — just `id`, `content`, and the vector — and translates
at the boundary: `BucketItemDocument` in, `BucketItemCandidate` out. `content`
is the item's title plus its item-type-relevant descriptive text (see
`tether.bucket_items.bucket_item_index_text`), not the raw payload. It knows
nothing about lifecycle state — the active-only scope is enforced upstream
against SQLite (which remains the canonical store; this index is disposable
and rebuildable).

>>> index = await BucketItemIndex.open(index_dir=Path(".tether/bucket-index"), vector_dim=384)
>>> await index.upsert([BucketItemDocument(id=item_id, content=text, vector=vec)])
>>> hits = await index.search(text="blade runner", vector=query_vec, limit=10)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from tether.hybrid_lance_table import HybridLanceTable, TableDocument

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_TABLE = "bucket_items"
"""The single table name inside the index dataset."""

_logger = structlog.stdlib.get_logger("tether.bucket_item_index")
"""Module logger for the salvage messages `optimize()` may emit.

The Bucket-item reconcile loop calls `optimize()` without a run-scoped logger,
so the salvage path logs under the module's own name instead."""


@dataclass(frozen=True, slots=True)
class BucketItemDocument:
    """A unit to (re)index: an id, its searchable text, and its embedding."""

    id: UUID
    content: str
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class BucketItemCandidate:
    """A hybrid-search hit: a Bucket-item id and its fused relevance score."""

    id: UUID
    score: float


def _to_table_document(document: BucketItemDocument) -> TableDocument:
    """Project a Bucket-item document onto the generic table row shape."""
    return TableDocument(
        id=document.id, content=document.content, vector=document.vector
    )


class BucketItemIndex:
    """Async hybrid retriever over the embedded LanceDB table of Bucket items."""

    def __init__(self, *, table: HybridLanceTable) -> None:
        self._table: HybridLanceTable = table

    @property
    def vector_dim(self) -> int:
        """Width of the vectors this index stores; fixes its schema."""
        return self._table.vector_dim

    @classmethod
    async def open(cls, *, index_dir: Path, vector_dim: int) -> BucketItemIndex:
        """Open the index at `index_dir`, creating the table if it is absent.

        Idempotent: an existing dataset is reused as-is. If its vector width
        disagrees with `vector_dim`, raises `VectorDimMismatchError` rather than
        corrupting the projection."""
        return cls(
            table=await HybridLanceTable.open(
                index_dir=index_dir, table_name=_TABLE, vector_dim=vector_dim
            )
        )

    async def upsert(self, documents: Sequence[BucketItemDocument]) -> None:
        """Insert or replace documents by id (content + vector)."""
        await self._table.upsert(
            [_to_table_document(document) for document in documents]
        )

    async def remove(self, ids: Sequence[UUID]) -> None:
        """Delete documents by id; ids absent from the index are ignored."""
        await self._table.remove(ids)

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[BucketItemCandidate]:
        """Hybrid search: native FTS on `text` + cosine on `vector`, fused by RRF."""
        hits = await self._table.search(text=text, vector=vector, limit=limit)
        return [BucketItemCandidate(id=hit.id, score=hit.score) for hit in hits]

    async def rebuild(self, documents: Sequence[BucketItemDocument]) -> None:
        """Drop the table and reindex `documents` from scratch.

        Used when the embedding model changes: vectors from different models are
        incomparable, so the whole projection is rebuilt from SQLite."""
        await self._table.rebuild(
            [_to_table_document(document) for document in documents]
        )

    async def count(self) -> int:
        """Number of documents currently indexed."""
        return await self._table.count()

    async def list_ids(self) -> set[UUID]:
        """Every document id currently in the index.

        The reconciler diffs this against SQLite's active-item set to drop
        orphans (rows whose Bucket item was completed/deleted or never indexed)
        left behind by a missed event — the correctness backstop for the
        latency path."""
        return await self._table.list_ids()

    async def optimize(self) -> None:
        """Run LanceDB's background hygiene (compaction, index maintenance).

        Delegates to the self-healing `HybridLanceTable.optimize`, which
        salvages the known lance compaction corruption in place instead of
        wedging the reconcile loop that drives this on every tick."""
        await self._table.optimize(logger=_logger)
