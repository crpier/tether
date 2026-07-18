"""Behavior tests for the Bucket-item search reconciler.

`BucketItemReconciler` is the sole writer that converges the Bucket-item
LanceDB index with SQLite (the canonical `bucket_item` rows). Unlike the
Memory reconciler it stores no vector back in SQLite — a `BucketItem`'s id is
stable and content-immutable once Added (no title-edit path exists yet), so an
id already present in the index is assumed current: only ids the index does
not already hold are (re-)embedded, and ids the index holds that are no longer
*active* (non-completed, non-deleted) are dropped as orphans.

Three paths are exercised here, mirroring `test_reconciler.py`:

- `reconcile()` — the idempotent full pass (startup + periodic): embed owed
  active items, upsert them, drop orphans, run `optimize()`, and stamp the
  active-model marker. A model change re-embeds every active item and rebuilds
  the index.
- `index_item` / `deindex_item` — the per-item latency hooks `BucketItemService`
  calls on Add / terminate (complete or delete).
- `candidates()` — the read path the service searches through: embed the
  query, run hybrid retrieval, return the RRF-ranked candidates unchanged.

These run against a real in-memory SQLite database with a `FakeBucketItemIndex`
(records writes) and a `CountingEmbedder` (proves what was re-embedded), plus
one end-to-end check against the *real* `BucketItemIndex`. No model download.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from anyio import TemporaryDirectory
from snekql.sqlite import Config, CurrentTimestamp, Database, Fetched, insert, update
from snektest import (
    assert_eq,
    assert_in,
    assert_is_not_none,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.bucket_item_index import (
    BucketItemCandidate,
    BucketItemDocument,
    BucketItemIndex,
)
from tether.bucket_item_reconciler import (
    BucketItemReconciler,
    BucketItemReconcileReport,
)
from tether.bucket_items import BucketItem, create_bucket_item_schema
from tether.embeddings import Embedder, FakeEmbedder, Vector
from tether.logging import Logger
from tether.search_meta import SearchMetaService, create_search_meta_schema

_DIM = 16


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.bucket_item_reconciler")


class FakeBucketItemIndex:
    """In-memory stand-in for `BucketItemIndex`; records every write for assertions."""

    def __init__(self, *, vector_dim: int) -> None:
        self._vector_dim: int = vector_dim
        self.docs: dict[UUID, BucketItemDocument] = {}
        self.optimize_calls: int = 0
        self.rebuilds: int = 0
        self.search_results: list[BucketItemCandidate] = []
        self.search_calls: list[tuple[str, Sequence[float], int]] = []

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[BucketItemCandidate]:
        self.search_calls.append((text, vector, limit))
        return self.search_results

    async def upsert(self, documents: Sequence[BucketItemDocument]) -> None:
        for document in documents:
            self.docs[document.id] = document

    async def remove(self, ids: Sequence[UUID]) -> None:
        for identifier in ids:
            _ = self.docs.pop(identifier, None)

    async def rebuild(self, documents: Sequence[BucketItemDocument]) -> None:
        self.rebuilds += 1
        self.docs = {document.id: document for document in documents}

    async def list_ids(self) -> set[UUID]:
        return set(self.docs)

    async def optimize(self) -> None:
        self.optimize_calls += 1


class CountingEmbedder:
    """Wraps an `Embedder`, counting how many documents it re-embeds."""

    def __init__(self, inner: Embedder) -> None:
        self._inner: Embedder = inner
        self.documents_embedded: int = 0

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def vector_dim(self) -> int:
        return self._inner.vector_dim

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.documents_embedded += len(texts)
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> Vector:
        return await self._inner.embed_query(text)


@dataclass
class Harness:
    reconciler: BucketItemReconciler
    database: Database
    index: FakeBucketItemIndex
    embedder: CountingEmbedder
    meta: SearchMetaService


async def _build_db() -> Database:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_bucket_item_schema(db)
    await create_search_meta_schema(db)
    return db


@fixture
async def harness() -> AsyncGenerator[Harness]:
    db = await _build_db()
    embedder = CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-a"))
    index = FakeBucketItemIndex(vector_dim=_DIM)
    meta = SearchMetaService(database=db)
    reconciler = BucketItemReconciler(
        database=db, index=index, embedder=embedder, meta=meta
    )
    yield Harness(reconciler, db, index, embedder, meta)
    await db.close()


async def _add_bucket_item(
    db: Database,
    title: str,
    *,
    completed: bool = False,
    deleted: bool = False,
) -> BucketItem[Fetched]:
    """Insert a Bucket item in the requested lifecycle state."""
    async with db.transaction() as tx:
        created = await tx.execute(
            insert(
                BucketItem(
                    item_type="movie",
                    title=title,
                    dedup_key=title.lower(),
                    data={"title": title},
                    intent_context="",
                )
            ).returning()
        )
        if completed:
            _ = await tx.execute(
                update(BucketItem)
                .set(BucketItem.completed_at.to(CurrentTimestamp))
                .where(BucketItem.id.eq(created.id))
            )
        if deleted:
            _ = await tx.execute(
                update(BucketItem)
                .set(BucketItem.deleted_at.to(CurrentTimestamp))
                .where(BucketItem.id.eq(created.id))
            )
    return created


@test()
async def reconcile_indexes_an_active_item() -> None:
    """An active Bucket item is embedded and pushed into the index."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 1)
    assert_in(item.id, set(h.index.docs))


@test()
async def reconcile_records_the_active_model_marker() -> None:
    """After a pass, `search_meta` names the model that produced the vectors."""
    h = await load_fixture(harness())
    _ = await h.reconciler.reconcile(logger=_logger())

    marker = await h.meta.fetch(logger=_logger())
    assert_is_not_none(marker)
    assert marker is not None
    assert_eq(marker.embedding_model, "fake-a")
    assert_eq(marker.vector_dim, _DIM)


@test()
async def completed_and_deleted_items_are_never_indexed() -> None:
    """Only active items enter the index or get embedded."""
    h = await load_fixture(harness())
    active = await _add_bucket_item(h.database, "active movie")
    completed = await _add_bucket_item(h.database, "completed movie", completed=True)
    deleted = await _add_bucket_item(h.database, "deleted movie", deleted=True)

    _ = await h.reconciler.reconcile(logger=_logger())

    assert_eq(set(h.index.docs), {active.id})
    assert_not_in(completed.id, set(h.index.docs))
    assert_not_in(deleted.id, set(h.index.docs))


@test()
async def reconcile_is_idempotent() -> None:
    """A second pass with no changes re-embeds nothing and leaves the index alone."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")
    _ = await h.reconciler.reconcile(logger=_logger())
    embedded_after_first = h.embedder.documents_embedded

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 0)
    assert_eq(h.embedder.documents_embedded, embedded_after_first)
    assert_eq(set(h.index.docs), {item.id})


@test()
async def reconcile_rebuilds_from_sqlite_after_a_wipe() -> None:
    """On restore the gitignored index is gone but SQLite survives; the pass
    refills the empty index by re-embedding the (still-owed) active items."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")
    _ = await h.reconciler.reconcile(logger=_logger())
    # Simulate a restore: drop the derived index, leave SQLite untouched.
    h.index.docs.clear()

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 1)
    assert_eq(set(h.index.docs), {item.id})


@test()
async def reconcile_drops_orphans_left_by_a_missed_event() -> None:
    """An index entry with no live active item behind it is removed."""
    h = await load_fixture(harness())
    live = await _add_bucket_item(h.database, "live item")
    _ = await h.reconciler.reconcile(logger=_logger())
    orphan = BucketItemDocument(id=uuid4(), content="ghost", vector=[0.0] * _DIM)
    h.index.docs[orphan.id] = orphan

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.removed, 1)
    assert_eq(set(h.index.docs), {live.id})


@test()
async def a_model_change_reembeds_the_corpus_and_rebuilds() -> None:
    """Swapping the embedding model re-embeds everything and rebuilds the index."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")
    _ = await h.reconciler.reconcile(logger=_logger())

    swapped = CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-b"))
    reconciler_b = BucketItemReconciler(
        database=h.database, index=h.index, embedder=swapped, meta=h.meta
    )
    report = await reconciler_b.reconcile(logger=_logger())

    assert_true(report.rebuilt)
    assert_eq(report.embedded, 1)
    assert_eq(h.index.rebuilds, 1)
    assert_in(item.id, set(h.index.docs))
    marker = await h.meta.fetch(logger=_logger())
    assert marker is not None
    assert_eq(marker.embedding_model, "fake-b")


@test()
async def reconcile_runs_optimize_each_pass() -> None:
    """Every reconcile runs the index's background hygiene."""
    h = await load_fixture(harness())
    _ = await _add_bucket_item(h.database, "Blade Runner")

    _ = await h.reconciler.reconcile(logger=_logger())
    _ = await h.reconciler.reconcile(logger=_logger())

    assert_eq(h.index.optimize_calls, 2)


@test()
async def reconcile_forever_runs_passes_until_cancelled() -> None:
    """The periodic loop keeps reconciling until cancelled.

    Cancellation must land while the loop is parked in its inter-pass sleep,
    never mid-pass: a real `snekql` transaction's `__aenter__` acquires a
    connection before it starts `BEGIN`, and has no `except CancelledError`
    handler to release it if cancellation lands on that awaited `BEGIN` (only
    `except Exception`, deliberately, so cancellation isn't swallowed) — so a
    cancel delivered mid-transaction leaks the pooled connection and the
    fixture's teardown `db.close()` then hangs until it times out
    (`DatabaseCloseTimeoutError`). `index.optimize_calls` ticks up *before* the
    pass's final transaction (`meta.set`), so counting on it (as the naive
    version of this test did) leaves that last transaction's `BEGIN` as an
    unsafe cancellation window and flakes intermittently. Counting completed
    passes instead — via a wrapper that increments only after the real
    `reconcile()` awaitable (and all its transactions) has returned — matches
    the safe pattern `test_reconcile_loop.py` uses: the only await between the
    signal and the next tick is `asyncio.sleep`, which cancels cleanly with no
    connection held.
    """
    h = await load_fixture(harness())
    _ = await _add_bucket_item(h.database, "Blade Runner")

    passes = 0
    real_reconcile = h.reconciler.reconcile

    async def _counting_reconcile(*, logger: Logger) -> BucketItemReconcileReport:
        nonlocal passes
        report = await real_reconcile(logger=logger)
        passes += 1
        return report

    h.reconciler.reconcile = _counting_reconcile

    task = asyncio.create_task(
        h.reconciler.reconcile_forever(interval_seconds=0.001, logger=_logger())
    )
    for _ in range(1000):  # bounded wait so a broken loop fails fast, never hangs
        if passes >= 1:
            break
        await asyncio.sleep(0.001)
    _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert_true(passes >= 1)
    assert_true(h.index.optimize_calls >= 1)


@test()
async def index_item_embeds_and_indexes_a_single_item() -> None:
    """The latency hook embeds an item and pushes it to the index."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")

    await h.reconciler.index_item(item, logger=_logger())

    assert_in(item.id, set(h.index.docs))


@test()
async def deindex_item_removes_a_single_item() -> None:
    """The terminate hook drops one Bucket item from the index."""
    h = await load_fixture(harness())
    item = await _add_bucket_item(h.database, "Blade Runner")
    _ = await h.reconciler.reconcile(logger=_logger())

    await h.reconciler.deindex_item(item.id, logger=_logger())

    assert_not_in(item.id, set(h.index.docs))


@test()
async def candidates_embeds_the_query_and_forwards_it_to_the_index() -> None:
    """The read path embeds the query once and hands it to the index with limit."""
    h = await load_fixture(harness())

    _ = await h.reconciler.candidates("blade runner", limit=7, logger=_logger())

    assert_eq(len(h.index.search_calls), 1)
    text, vector, limit = h.index.search_calls[0]
    assert_eq(text, "blade runner")
    assert_eq(limit, 7)
    expected = await h.embedder.embed_query("blade runner")
    assert_eq(list(vector), expected)


@test()
async def candidates_returns_the_index_ranking_unchanged() -> None:
    """The read path is a pass-through for ranking; it never reorders candidates."""
    h = await load_fixture(harness())
    ranked = [
        BucketItemCandidate(id=uuid4(), score=0.9),
        BucketItemCandidate(id=uuid4(), score=0.4),
    ]
    h.index.search_results = ranked

    result = await h.reconciler.candidates("anything", limit=10, logger=_logger())

    assert_eq([candidate.id for candidate in result], [c.id for c in ranked])


@test()
async def reconcile_converges_the_real_bucket_item_index() -> None:
    """End-to-end against the real LanceDB adapter: the active item is found,
    completed and deleted ones never are."""
    async with TemporaryDirectory() as tmp:
        db = await _build_db()
        embedder = FakeEmbedder(vector_dim=_DIM, model_name="fake-a")
        index = await BucketItemIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        meta = SearchMetaService(database=db)
        reconciler = BucketItemReconciler(
            database=db, index=index, embedder=embedder, meta=meta
        )
        active = await _add_bucket_item(db, "Blade Runner")
        completed = await _add_bucket_item(db, "Watched Movie", completed=True)
        deleted = await _add_bucket_item(db, "Rejected Movie", deleted=True)

        _ = await reconciler.reconcile(logger=_logger())

        assert_eq(await index.count(), 1)
        candidates = await index.search(
            text="Blade Runner",
            vector=await embedder.embed_query("Blade Runner"),
            limit=10,
        )
        found = {candidate.id for candidate in candidates}
        assert_in(active.id, found)
        assert_not_in(completed.id, found)
        assert_not_in(deleted.id, found)
        await db.close()
