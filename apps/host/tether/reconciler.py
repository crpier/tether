"""The search reconciler: the sole writer that converges LanceDB with SQLite.

A tethered Memory has three derived artifacts, all governed identically and at
the same trigger points: the markdown projection (owned by `KnowledgeBaseService`),
the embedding vector (a SQLite BLOB), and the LanceDB index entry. This module
owns the latter two together. SQLite is canonical; LanceDB is disposable and
rebuildable, so a second derived store that can't share SQLite's transaction is
reconciled rather than dual-written.

Two paths keep the index honest:

- `reconcile` is the idempotent full pass run at startup and periodically. It
  embeds any owed `tethered ∧ ¬deleted` Memory (storing the vector in SQLite),
  upserts that desired set into the index, drops orphans an event missed, runs
  `optimize()`, and records the active embedding model in `search_meta`. A model
  change (the marker disagrees with the live `Embedder`) re-embeds the whole
  corpus and rebuilds the index, since vector spaces from different models can't
  be compared. This pass alone is enough — on restore the gitignored index is
  recreated empty and refilled from SQLite's stored vectors with no re-embed.
- `index_memory` / `deindex_memory` are the per-Memory latency hooks the Review
  spine calls at tether / edit / delete so a change is searchable immediately,
  without waiting for the next pass. The pass is their correctness backstop.

The reconciler talks to the index only through `SearchIndexPort` and to the model
only through `Embedder`, so it is fully testable against fakes of both.

>>> reconciler = SearchReconciler(
...     database=database, index=index, embedder=embedder, meta=meta
... )
>>> report = await reconciler.reconcile(logger=logger)
>>> report.embedded
0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from snekql.sqlite import select, update

from tether.embeddings import vector_from_bytes, vector_to_bytes
from tether.memories import Memory
from tether.search_index import SearchDocument

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from snekql.sqlite import Database, Fetched, Transaction

    from tether.embeddings import Embedder, Vector
    from tether.logging import Logger
    from tether.search_meta import SearchMetaService


class SearchIndexPort(Protocol):
    """The slice of `SearchIndex` the reconciler writes through.

    A Protocol, not the concrete class, so the reconciler can be driven by a
    fake in tests; `SearchIndex` satisfies it structurally."""

    @property
    def vector_dim(self) -> int: ...
    async def upsert(self, documents: Sequence[SearchDocument]) -> None: ...
    async def remove(self, ids: Sequence[UUID]) -> None: ...
    async def rebuild(self, documents: Sequence[SearchDocument]) -> None: ...
    async def list_ids(self) -> set[UUID]: ...
    async def optimize(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What a reconcile pass did, for logging and tests."""

    embedded: int
    """Memories (re)embedded this pass."""
    indexed: int
    """Documents upserted into the index (the full desired set)."""
    removed: int
    """Orphan index entries dropped (no live Memory behind them)."""
    rebuilt: bool
    """Whether a model change forced a full drop-and-rebuild."""


class SearchReconciler:
    """Converges the LanceDB index with SQLite; the sole index writer."""

    def __init__(
        self,
        *,
        database: Database,
        index: SearchIndexPort,
        embedder: Embedder,
        meta: SearchMetaService,
    ) -> None:
        if index.vector_dim != embedder.vector_dim:
            message = (
                f"index vector width {index.vector_dim} does not match embedder "
                f"width {embedder.vector_dim}"
            )
            raise ValueError(message)
        self.database: Database = database
        self.index: SearchIndexPort = index
        self.embedder: Embedder = embedder
        self.meta: SearchMetaService = meta

    async def reconcile(self, *, logger: Logger) -> ReconcileReport:
        """Bring the index in step with SQLite; idempotent.

        On a model change every vector is recomputed and the index rebuilt;
        otherwise only owed Memories are embedded and the index is converged in
        place (upsert the desired set, drop orphans)."""
        marker = await self.meta.get(logger=logger)
        # A *genuine* model swap (marker present but disagreeing) discards every
        # vector and rebuilds. A missing marker is just a first/restore run: the
        # incremental path populates the empty index, re-embedding owed rows
        # (those carry no vector yet) without a needless drop.
        model_changed = marker is not None and (
            marker.embedding_model != self.embedder.model_name
            or marker.vector_dim != self.embedder.vector_dim
        )
        logger.debug(
            "Reconciling search index",
            model=self.embedder.model_name,
            model_changed=model_changed,
        )

        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(
                select(Memory).where(
                    Memory.tethered_at.is_not_null() & Memory.deleted_at.is_null()
                )
            )
        owed = [
            memory
            for memory in memories
            if model_changed
            or memory.embedding is None
            or memory.embedded_version != memory.version
        ]
        fresh_vectors = await self._embed_owed(owed)

        documents: list[SearchDocument] = []
        for memory in memories:
            vector = fresh_vectors.get(memory.id)
            if vector is None:
                stored = memory.embedding
                assert stored is not None  # a non-owed Memory always has a vector
                vector = vector_from_bytes(stored)
            documents.append(
                SearchDocument(id=memory.id, content=memory.content, vector=vector)
            )

        if model_changed:
            await self.index.rebuild(documents)
            removed = 0
        else:
            await self.index.upsert(documents)
            removed = await self._drop_orphans(memories)
        await self.index.optimize()
        _ = await self.meta.set(
            model=self.embedder.model_name,
            vector_dim=self.embedder.vector_dim,
            logger=logger,
        )
        report = ReconcileReport(
            embedded=len(owed),
            indexed=len(documents),
            removed=removed,
            rebuilt=model_changed,
        )
        logger.info(
            "Search index reconciled",
            embedded=report.embedded,
            indexed=report.indexed,
            removed=report.removed,
            rebuilt=report.rebuilt,
        )
        return report

    async def index_memory(self, memory: Memory[Fetched], *, logger: Logger) -> None:
        """Make a single tethered Memory searchable now (the tether/edit hook).

        Embeds it if its vector is missing or stale, persists that vector, then
        upserts the one document — the low-latency complement to `reconcile`."""
        logger.debug("Indexing Memory", memory_id=str(memory.id))
        if memory.embedding is not None and memory.embedded_version == memory.version:
            vector = vector_from_bytes(memory.embedding)
        else:
            vector = (await self.embedder.embed_documents([memory.content]))[0]
            async with self.database.transaction() as tx:
                await self._store_embedding(tx, memory.id, memory.version, vector)
        await self.index.upsert(
            [SearchDocument(id=memory.id, content=memory.content, vector=vector)]
        )

    async def deindex_memory(self, memory_id: UUID, *, logger: Logger) -> None:
        """Drop a single Memory from the index (the delete hook)."""
        logger.debug("Deindexing Memory", memory_id=str(memory_id))
        await self.index.remove([memory_id])

    async def _embed_owed(self, owed: Sequence[Memory[Fetched]]) -> dict[UUID, Vector]:
        """Embed the owed Memories in one batch and persist each vector.

        Embedding runs outside any transaction so a slow model never holds a DB
        lock; the vectors are then written back guarded by content version."""
        if not owed:
            return {}
        vectors = await self.embedder.embed_documents(
            [memory.content for memory in owed]
        )
        fresh: dict[UUID, Vector] = {}
        async with self.database.transaction() as tx:
            for memory, vector in zip(owed, vectors, strict=True):
                await self._store_embedding(tx, memory.id, memory.version, vector)
                fresh[memory.id] = vector
        return fresh

    async def _drop_orphans(self, desired: Sequence[Memory[Fetched]]) -> int:
        """Remove index entries with no live Memory behind them."""
        desired_ids = {memory.id for memory in desired}
        orphans = [
            identifier
            for identifier in await self.index.list_ids()
            if identifier not in desired_ids
        ]
        if orphans:
            await self.index.remove(orphans)
        return len(orphans)

    @staticmethod
    async def _store_embedding(
        tx: Transaction, memory_id: UUID, version: int, vector: Vector
    ) -> None:
        """Write a vector + its content version, only if the content still matches.

        The `version` guard means a concurrent edit (which bumps `version`) is
        never overwritten with a stale vector — that Memory simply stays owed for
        the next pass."""
        _ = await tx.execute(
            update(Memory)
            .set(Memory.embedding.to(vector_to_bytes(vector)))
            .set(Memory.embedded_version.to(version))
            .where(Memory.id.eq(memory_id))
            .where(Memory.version.eq(version))
        )
