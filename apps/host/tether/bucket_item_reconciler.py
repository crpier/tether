"""The Bucket-item reconciler: converges the LanceDB index with SQLite.

A sibling of `SearchReconciler` (Memories) over the same generic hybrid index
mechanics, adapted for a corpus that has no derived `embedding` column of its
own: unlike a Memory, a `BucketItem` never stores its vector back in SQLite, so
this reconciler leans entirely on the index's own `list_ids()` — an id already
present there is assumed current, since the only mutations that change a
Bucket item's indexed text (`bucket_item_index_text`: title + item-type text)
are Add (which always produces a brand-new id) and terminate (which removes the
id from scope entirely). There is no in-place title edit today, so "id present
⇒ content current" holds; a future edit path would need to force a re-embed
(e.g. by removing the id first) the way a Memory edit bumps `embedded_version`.

Three paths keep the index honest and readable, mirroring `SearchReconciler`:

- `reconcile` is the idempotent full pass run at startup and periodically. It
  fetches every *active* (non-completed, non-deleted) Bucket item, embeds
  whichever ids the index does not already hold, upserts those, drops orphans
  an event missed, runs `optimize()`, and records the active embedding model in
  `search_meta` (the same singleton marker the Memory reconciler writes, since
  both arms share one embedder). A model change re-embeds every active item and
  rebuilds the index, since vector spaces from different models can't be
  compared.
- `index_item` / `deindex_item` are the per-item latency hooks
  `BucketItemService` calls at Add / terminate (complete or delete) so a change
  is searchable immediately, without waiting for the next pass. The pass is
  their correctness backstop.
- `candidates` is the read path: embed the query, run hybrid search, return the
  RRF-ranked `(id, score)` candidates. It deliberately does *not* filter by
  lifecycle state — `BucketItemService` re-filters the candidates against
  SQLite, which is where the active-only invariant is enforced. Enforcing it
  upstream of the index means a drifted index (an orphan a missed event left
  behind) can never leak a completed or deleted item into a result.

The reconciler talks to the index only through `BucketItemIndexPort` and to the
model only through `Embedder`, so it is fully testable against fakes of both.

>>> reconciler = BucketItemReconciler(
...     database=database, index=index, embedder=embedder, meta=meta
... )
>>> report = await reconciler.reconcile(logger=logger)
>>> report.embedded
0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from snekql.sqlite import select

from tether.bucket_item_index import BucketItemDocument
from tether.bucket_items import BucketItem, bucket_item_index_text
from tether.reconcile_loop import run_reconcile_loop

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from snekql.sqlite import Database, Fetched, SelectModelQuery

    from tether.bucket_item_index import BucketItemCandidate
    from tether.embeddings import Embedder
    from tether.logging import Logger
    from tether.search_meta import SearchMetaService


class EmbedderIndexMismatchError(Exception):
    """Raised when the embedder and index disagree on vector width.

    They share one fixed-size vector space: an embedder producing N-wide
    vectors can only write to an index built for N. A mismatch is a wiring
    error, not a drift the reconciler can repair, so it is refused at
    construction."""


class BucketItemIndexPort(Protocol):
    """The slice of `BucketItemIndex` the reconciler reads and writes through.

    A Protocol, not the concrete class, so the reconciler can be driven by a
    fake in tests; `BucketItemIndex` satisfies it structurally."""

    @property
    def vector_dim(self) -> int: ...
    async def upsert(self, documents: Sequence[BucketItemDocument]) -> None: ...
    async def remove(self, ids: Sequence[UUID]) -> None: ...
    async def rebuild(self, documents: Sequence[BucketItemDocument]) -> None: ...
    async def list_ids(self) -> set[UUID]: ...
    async def optimize(self) -> None: ...
    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[BucketItemCandidate]: ...


@dataclass(frozen=True, slots=True)
class BucketItemReconcileReport:
    """What a reconcile pass did, for logging and tests."""

    embedded: int
    """Bucket items (re)embedded this pass."""
    indexed: int
    """Active items in the desired set this pass converged toward."""
    removed: int
    """Orphan index entries dropped (no active item behind them)."""
    rebuilt: bool
    """Whether a model change forced a full drop-and-rebuild."""


def _active_corpus() -> SelectModelQuery[BucketItem, BucketItem[Fetched]]:
    """The active-item corpus: non-completed, non-deleted Bucket items."""
    return select(BucketItem).where(
        BucketItem.completed_at.is_null() & BucketItem.deleted_at.is_null()
    )


class BucketItemReconciler:
    """Converges the LanceDB index with SQLite; the sole index writer."""

    def __init__(
        self,
        *,
        database: Database,
        index: BucketItemIndexPort,
        embedder: Embedder,
        meta: SearchMetaService,
    ) -> None:
        if index.vector_dim != embedder.vector_dim:
            message = (
                f"index vector width {index.vector_dim} does not match embedder "
                f"width {embedder.vector_dim}"
            )
            raise EmbedderIndexMismatchError(message)
        self.database: Database = database
        self.index: BucketItemIndexPort = index
        self.embedder: Embedder = embedder
        self.meta: SearchMetaService = meta

    async def reconcile(self, *, logger: Logger) -> BucketItemReconcileReport:
        """Bring the index in step with SQLite; idempotent.

        On a model change every active item is re-embedded and the index
        rebuilt; otherwise only ids the index does not already hold are
        embedded and upserted, and orphans (ids the index holds that are no
        longer active) are dropped."""
        marker = await self.meta.fetch(logger=logger)
        # A *genuine* model swap (marker present but disagreeing) discards
        # every vector and rebuilds. A missing marker is just a first/restore
        # run: the incremental path populates the empty index from scratch.
        model_changed = marker is not None and (
            marker.embedding_model != self.embedder.model_name
            or marker.vector_dim != self.embedder.vector_dim
        )
        logger.debug(
            "Reconciling Bucket item search index",
            model=self.embedder.model_name,
            model_changed=model_changed,
        )

        async with self.database.transaction() as tx:
            items = await tx.fetch_all(_active_corpus())

        if model_changed:
            documents = await self._embed(items)
            await self.index.rebuild(documents)
            embedded = len(items)
            removed = 0
        else:
            present = await self.index.list_ids()
            owed = [item for item in items if item.id not in present]
            if owed:
                await self.index.upsert(await self._embed(owed))
            desired_ids = {item.id for item in items}
            orphans = [
                identifier for identifier in present if identifier not in desired_ids
            ]
            if orphans:
                await self.index.remove(orphans)
            embedded = len(owed)
            removed = len(orphans)

        await self.index.optimize()
        _ = await self.meta.set(
            model=self.embedder.model_name,
            vector_dim=self.embedder.vector_dim,
            logger=logger,
        )
        report = BucketItemReconcileReport(
            embedded=embedded,
            indexed=len(items),
            removed=removed,
            rebuilt=model_changed,
        )
        logger.info(
            "Bucket item search index reconciled",
            embedded=report.embedded,
            indexed=report.indexed,
            removed=report.removed,
            rebuilt=report.rebuilt,
        )
        return report

    async def reconcile_forever(
        self, *, interval_seconds: float, logger: Logger
    ) -> None:
        """Run `reconcile` on a fixed interval until cancelled.

        This is the correctness backstop the latency hooks lean on: it sweeps
        orphans a missed event left behind and runs `optimize()` while the host
        is up, not only at boot. The boot reconcile runs at wiring time, so the
        first periodic pass waits a full interval rather than repeating it
        immediately. A failed pass is logged and swallowed so a transient error
        never kills the loop — the next tick retries."""
        await run_reconcile_loop(
            lambda: self.reconcile(logger=logger),
            interval_seconds=interval_seconds,
            initial_delay_seconds=interval_seconds,
            logger=logger,
            failure_message=(
                "Periodic Bucket item search reconcile failed; retrying next tick"
            ),
        )

    async def index_item(self, item: BucketItem[Fetched], *, logger: Logger) -> None:
        """Make a single active Bucket item searchable now (the Add hook)."""
        logger.debug("Indexing Bucket item", bucket_item_id=str(item.id))
        vector = (await self.embedder.embed_documents([bucket_item_index_text(item)]))[
            0
        ]
        await self.index.upsert(
            [
                BucketItemDocument(
                    id=item.id, content=bucket_item_index_text(item), vector=vector
                )
            ]
        )

    async def deindex_item(self, item_id: UUID, *, logger: Logger) -> None:
        """Drop a single Bucket item from the index (the terminate hook)."""
        logger.debug("Deindexing Bucket item", bucket_item_id=str(item_id))
        await self.index.remove([item_id])

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[BucketItemCandidate]:
        """Embed the query and return RRF-ranked candidates, unfiltered by state.

        The read complement to the write hooks: `BucketItemService` calls this
        and re-filters the returned ids against SQLite, so the index's
        lifecycle drift can never surface a completed or deleted item."""
        logger.debug("Embedding Bucket item search query", query_length=len(query))
        vector = await self.embedder.embed_query(query)
        return await self.index.search(text=query, vector=vector, limit=limit)

    async def _embed(
        self, items: Sequence[BucketItem[Fetched]]
    ) -> list[BucketItemDocument]:
        """Embed a batch of Bucket items into upsert-ready documents."""
        if not items:
            return []
        texts = [bucket_item_index_text(item) for item in items]
        vectors = await self.embedder.embed_documents(texts)
        return [
            BucketItemDocument(id=item.id, content=text, vector=vector)
            for item, text, vector in zip(items, texts, vectors, strict=True)
        ]
