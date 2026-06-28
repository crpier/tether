"""Behavior tests for the search reconciler (slice 4).

The reconciler is the *sole* LanceDB writer: it converges the derived index
with SQLite, the canonical store. It governs the two non-markdown derived
artifacts of a tethered Memory together — the embedding vector (a SQLite BLOB)
and the LanceDB index entry — at the same trigger points.

Two paths are exercised here:

- `reconcile()` — the idempotent, marker-driven full pass (startup + periodic):
  embed any owed `tethered ∧ ¬deleted` Memory, upsert the desired set, drop
  orphans left by a missed event, run `optimize()`, and stamp the active-model
  marker. A model change re-embeds the whole corpus and rebuilds the index.
- `index_memory` / `deindex_memory` — the per-Memory latency hooks slice 5 wires
  into tether / edit / delete so a change is searchable without waiting for a
  pass.

These run against a real in-memory SQLite database with a `FakeSearchIndex`
(records writes) and a `CountingEmbedder` (proves what was re-embedded), plus one
end-to-end check against the *real* `SearchIndex`. No model download, no network.
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
from snekql.sqlite import (
    Config,
    CurrentTimestamp,
    Database,
    Fetched,
    insert,
    select,
    update,
)
from snektest import (
    assert_eq,
    assert_in,
    assert_is_none,
    assert_is_not_none,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.embeddings import (
    Embedder,
    FakeEmbedder,
    Vector,
    vector_from_bytes,
    vector_to_bytes,
)
from tether.logging import Logger
from tether.memories import Memory, create_memory_schema
from tether.reconciler import SearchReconciler
from tether.search_index import SearchDocument, SearchIndex
from tether.search_meta import SearchMetaService, create_search_meta_schema

_DIM = 16


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.reconciler")


def _stored(vector: Vector) -> Vector:
    """A vector as it reads back after float32 SQLite serialization."""
    return vector_from_bytes(vector_to_bytes(vector))


class FakeSearchIndex:
    """In-memory stand-in for `SearchIndex`; records every write for assertions."""

    def __init__(self, *, vector_dim: int) -> None:
        self._vector_dim: int = vector_dim
        self.docs: dict[UUID, SearchDocument] = {}
        self.optimize_calls: int = 0
        self.rebuilds: int = 0

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    async def upsert(self, documents: Sequence[SearchDocument]) -> None:
        for document in documents:
            self.docs[document.id] = document

    async def remove(self, ids: Sequence[UUID]) -> None:
        for identifier in ids:
            _ = self.docs.pop(identifier, None)

    async def rebuild(self, documents: Sequence[SearchDocument]) -> None:
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
    reconciler: SearchReconciler
    database: Database
    index: FakeSearchIndex
    embedder: CountingEmbedder
    meta: SearchMetaService


async def _build_db() -> Database:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_search_meta_schema(db)
    return db


@fixture
async def harness() -> AsyncGenerator[Harness]:
    db = await _build_db()
    embedder = CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-a"))
    index = FakeSearchIndex(vector_dim=_DIM)
    meta = SearchMetaService(database=db)
    reconciler = SearchReconciler(
        database=db, index=index, embedder=embedder, meta=meta
    )
    yield Harness(reconciler, db, index, embedder, meta)
    await db.close()


async def _add_memory(
    db: Database, content: str, *, tethered: bool = True, deleted: bool = False
) -> Memory[Fetched]:
    """Insert a Memory in the requested state, mirroring the real mutations."""
    async with db.transaction() as tx:
        created = await tx.execute(insert(Memory(content=content)).returning())
        if tethered:
            _ = await tx.execute(
                update(Memory)
                .set(Memory.tethered_at.to(CurrentTimestamp))
                .where(Memory.id.eq(created.id))
            )
        if deleted:
            _ = await tx.execute(
                update(Memory)
                .set(Memory.deleted_at.to(CurrentTimestamp))
                .where(Memory.id.eq(created.id))
            )
        fresh = await tx.fetch_one_or_none(
            select(Memory).where(Memory.id.eq(created.id))
        )
    assert fresh is not None
    return fresh


async def _fetch(db: Database, memory_id: UUID) -> Memory[Fetched]:
    async with db.transaction() as tx:
        memory = await tx.fetch_one_or_none(
            select(Memory).where(Memory.id.eq(memory_id))
        )
    assert memory is not None
    return memory


async def _edit_content(db: Database, memory: Memory[Fetched], content: str) -> None:
    """Bump content + version like an edit, leaving `embedded_version` stale."""
    async with db.transaction() as tx:
        _ = await tx.execute(
            update(Memory)
            .set(Memory.content.to(content))
            .set(Memory.version.to(memory.version + 1))
            .where(Memory.id.eq(memory.id))
        )


@test()
async def reconcile_indexes_a_tethered_memory() -> None:
    """A tethered Memory is embedded and pushed into the index."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment tuesday")

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 1)
    assert_in(memory.id, set(h.index.docs))


@test()
async def reconcile_persists_the_embedding_to_sqlite() -> None:
    """The vector the pass computes is written back to the canonical SQLite row."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment tuesday")

    _ = await h.reconciler.reconcile(logger=_logger())

    fresh = await _fetch(h.database, memory.id)
    assert_is_not_none(fresh.embedding)
    assert fresh.embedding is not None
    assert_eq(fresh.embedded_version, fresh.version)
    expected = (await h.embedder.embed_documents(["dentist appointment tuesday"]))[0]
    assert_eq(vector_from_bytes(fresh.embedding), _stored(expected))


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
async def loose_and_deleted_memories_are_never_indexed() -> None:
    """Only `tethered ∧ ¬deleted` Memories enter the index or get embedded."""
    h = await load_fixture(harness())
    tethered = await _add_memory(h.database, "tethered fact")
    loose = await _add_memory(h.database, "loose note", tethered=False)
    deleted = await _add_memory(h.database, "rejected note", deleted=True)

    _ = await h.reconciler.reconcile(logger=_logger())

    assert_eq(set(h.index.docs), {tethered.id})
    # A loose Memory has no derived artifacts — not even an embedding.
    assert_is_none((await _fetch(h.database, loose.id)).embedding)
    assert_not_in(deleted.id, set(h.index.docs))


@test()
async def reconcile_is_idempotent() -> None:
    """A second pass with no changes re-embeds nothing and leaves the index alone."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())
    embedded_after_first = h.embedder.documents_embedded

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 0)
    assert_eq(h.embedder.documents_embedded, embedded_after_first)
    assert_eq(set(h.index.docs), {memory.id})


@test()
async def reconcile_rebuilds_the_index_from_sqlite_without_reembedding() -> None:
    """On restore the gitignored index is gone but SQLite (vectors + marker)
    survives; the pass refills the empty index from stored vectors, no re-embed."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())
    embedded_after_first = h.embedder.documents_embedded
    # Simulate a restore: drop the derived index, leave SQLite untouched.
    h.index.docs.clear()

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 0)
    assert_eq(h.embedder.documents_embedded, embedded_after_first)
    assert_eq(set(h.index.docs), {memory.id})


@test()
async def reconcile_drops_orphans_left_by_a_missed_event() -> None:
    """An index entry with no live Memory behind it is removed — drift repair."""
    h = await load_fixture(harness())
    live = await _add_memory(h.database, "live fact")
    _ = await h.reconciler.reconcile(logger=_logger())
    orphan = SearchDocument(id=uuid4(), content="ghost", vector=[0.0] * _DIM)
    h.index.docs[orphan.id] = orphan

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.removed, 1)
    assert_eq(set(h.index.docs), {live.id})


@test()
async def reconcile_reembeds_edited_content() -> None:
    """A Memory whose content changed since its vector is re-embedded."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())
    await _edit_content(h.database, memory, "optometrist appointment")

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.embedded, 1)
    fresh = await _fetch(h.database, memory.id)
    assert_eq(fresh.embedded_version, fresh.version)
    assert fresh.embedding is not None
    expected = (await h.embedder.embed_documents(["optometrist appointment"]))[0]
    assert_eq(vector_from_bytes(fresh.embedding), _stored(expected))


@test()
async def a_model_change_reembeds_the_corpus_and_rebuilds() -> None:
    """Swapping the embedding model re-embeds everything and rebuilds the index."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())

    swapped = CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-b"))
    reconciler_b = SearchReconciler(
        database=h.database, index=h.index, embedder=swapped, meta=h.meta
    )
    report = await reconciler_b.reconcile(logger=_logger())

    assert_true(report.rebuilt)
    assert_eq(report.embedded, 1)
    assert_eq(h.index.rebuilds, 1)
    assert_in(memory.id, set(h.index.docs))
    marker = await h.meta.fetch(logger=_logger())
    assert marker is not None
    assert_eq(marker.embedding_model, "fake-b")


@test()
async def reconcile_runs_optimize_each_pass() -> None:
    """Every reconcile runs the index's background hygiene."""
    h = await load_fixture(harness())
    _ = await _add_memory(h.database, "dentist appointment")

    _ = await h.reconciler.reconcile(logger=_logger())
    _ = await h.reconciler.reconcile(logger=_logger())

    assert_eq(h.index.optimize_calls, 2)


@test()
async def reconcile_forever_runs_passes_until_cancelled() -> None:
    """The periodic loop keeps reconciling (the correctness backstop) until cancelled."""
    h = await load_fixture(harness())
    _ = await _add_memory(h.database, "dentist appointment")

    task = asyncio.create_task(
        h.reconciler.reconcile_forever(interval_seconds=0.001, logger=_logger())
    )
    for _ in range(1000):  # bounded wait so a broken loop fails fast, never hangs
        if h.index.optimize_calls >= 1:
            break
        await asyncio.sleep(0.001)
    _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert_true(h.index.optimize_calls >= 1)


@test()
async def index_memory_embeds_and_indexes_a_single_memory() -> None:
    """The latency hook embeds an owed Memory and pushes it to the index."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")

    await h.reconciler.index_memory(memory, logger=_logger())

    assert_in(memory.id, set(h.index.docs))
    fresh = await _fetch(h.database, memory.id)
    assert fresh.embedding is not None
    assert_eq(fresh.embedded_version, fresh.version)


@test()
async def index_memory_reuses_a_fresh_embedding() -> None:
    """A Memory already embedded at its current version is not re-embedded."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())
    embedded_before = h.embedder.documents_embedded

    await h.reconciler.index_memory(
        await _fetch(h.database, memory.id), logger=_logger()
    )

    assert_eq(h.embedder.documents_embedded, embedded_before)
    assert_in(memory.id, set(h.index.docs))


@test()
async def deindex_memory_removes_a_single_memory() -> None:
    """The delete hook drops one Memory from the index."""
    h = await load_fixture(harness())
    memory = await _add_memory(h.database, "dentist appointment")
    _ = await h.reconciler.reconcile(logger=_logger())

    await h.reconciler.deindex_memory(memory.id, logger=_logger())

    assert_not_in(memory.id, set(h.index.docs))


@test()
async def reconcile_converges_the_real_search_index() -> None:
    """End-to-end against the real LanceDB adapter: the tethered Memory is found,
    loose and deleted ones never are."""
    async with TemporaryDirectory() as tmp:
        db = await _build_db()
        embedder = FakeEmbedder(vector_dim=_DIM, model_name="fake-a")
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        meta = SearchMetaService(database=db)
        reconciler = SearchReconciler(
            database=db, index=index, embedder=embedder, meta=meta
        )
        tethered = await _add_memory(db, "dentist appointment tuesday")
        loose = await _add_memory(db, "grocery shopping list", tethered=False)
        deleted = await _add_memory(db, "old rejected note", deleted=True)

        _ = await reconciler.reconcile(logger=_logger())

        assert_eq(await index.count(), 1)
        candidates = await index.search(
            text="dentist", vector=await embedder.embed_query("dentist"), limit=10
        )
        found = {candidate.id for candidate in candidates}
        assert_in(tethered.id, found)
        assert_not_in(loose.id, found)
        assert_not_in(deleted.id, found)
        await db.close()
