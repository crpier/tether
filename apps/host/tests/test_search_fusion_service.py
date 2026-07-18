"""Behavior tests for `SearchFusionService`, the cross-source Search seam.

Drives the real service seam — `MemoryService` and `BucketItemService` over a
real in-memory SQLite database, each wired to its real reconciler
(`SearchReconciler` / `BucketItemReconciler`) and a `SearchIndexPort`-shaped
fake index (`FakeSearchIndex` / `FakeBucketItemIndex`, mirroring
`test_reconciler.py` / `test_bucket_item_reconciler.py`) plus a `FakeEmbedder`.
No real LanceDB, no real embedding model.

`SearchFusionService.search` is the fused seam under test: each arm's raw
candidates (set directly on its fake index's `search_results`, so ranking is
fully controlled) are hydrated and re-filtered through its own service before
fusion ever sees them — the behavior `tether.search_fusion.fuse()`'s own tests
(`test_search_fusion.py`) assume holds true for these real arms too.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid7

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, insert
from snektest import (
    assert_eq,
    assert_not_in,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.bucket_item_index import BucketItemCandidate, BucketItemDocument
from tether.bucket_item_reconciler import BucketItemReconciler
from tether.bucket_items import BucketItemService, create_bucket_item_schema
from tether.embeddings import FakeEmbedder
from tether.logging import Logger
from tether.memories import (
    EmptySearchQueryError,
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.recall import StudyItem, StudyItemState, create_recall_schema
from tether.reconciler import SearchReconciler
from tether.search_fusion import FusedHit, SearchFusionService
from tether.search_index import SearchCandidate, SearchDocument
from tether.search_meta import SearchMetaService, create_search_meta_schema

_DIM = 16


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.search_fusion_service")


def _noop_tracer() -> Tracer:
    return trace.NoOpTracerProvider().get_tracer("test.search_fusion_service")


class FakeSearchIndex:
    """A `SearchIndexPort`-shaped fake: search results are set directly."""

    def __init__(self, *, vector_dim: int) -> None:
        self._vector_dim: int = vector_dim
        self.search_results: list[SearchCandidate] = []

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[SearchCandidate]:
        del text, vector, limit
        return self.search_results

    async def upsert(self, documents: Sequence[SearchDocument]) -> None:
        del documents

    async def remove(self, ids: Sequence[UUID]) -> None:
        del ids

    async def rebuild(self, documents: Sequence[SearchDocument]) -> None:
        del documents

    async def list_ids(self) -> set[UUID]:
        return set()

    async def optimize(self) -> None:
        pass


class FakeBucketItemIndex:
    """A `BucketItemIndexPort`-shaped fake: search results are set directly."""

    def __init__(self, *, vector_dim: int) -> None:
        self._vector_dim: int = vector_dim
        self.search_results: list[BucketItemCandidate] = []

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[BucketItemCandidate]:
        del text, vector, limit
        return self.search_results

    async def upsert(self, documents: Sequence[BucketItemDocument]) -> None:
        del documents

    async def remove(self, ids: Sequence[UUID]) -> None:
        del ids

    async def rebuild(self, documents: Sequence[BucketItemDocument]) -> None:
        del documents

    async def list_ids(self) -> set[UUID]:
        return set()

    async def optimize(self) -> None:
        pass


@dataclass
class Harness:
    """A `SearchFusionService` over real services, with both fake indexes exposed
    so tests can set each arm's raw candidates directly."""

    fusion: SearchFusionService
    memory_service: MemoryService
    bucket_item_service: BucketItemService
    memory_index: FakeSearchIndex
    bucket_item_index: FakeBucketItemIndex


@fixture
async def harness() -> AsyncGenerator[Harness]:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_bucket_item_schema(db)
    await create_search_meta_schema(db)
    await create_recall_schema(db)
    meta = SearchMetaService(database=db)
    memory_index = FakeSearchIndex(vector_dim=_DIM)
    bucket_item_index = FakeBucketItemIndex(vector_dim=_DIM)
    memory_reconciler = SearchReconciler(
        database=db,
        index=memory_index,
        embedder=FakeEmbedder(vector_dim=_DIM),
        meta=meta,
    )
    bucket_item_reconciler = BucketItemReconciler(
        database=db,
        index=bucket_item_index,
        embedder=FakeEmbedder(vector_dim=_DIM),
        meta=meta,
    )
    async with TemporaryDirectory() as kb_root:
        memory_service = MemoryService(
            database=db,
            kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
            tracer=_noop_tracer(),
            searcher=memory_reconciler,
        )
        bucket_item_service = BucketItemService(
            database=db,
            tracer=_noop_tracer(),
            searcher=bucket_item_reconciler,
        )
        fusion = SearchFusionService(
            memory_service=memory_service, bucket_item_service=bucket_item_service
        )
        yield Harness(
            fusion, memory_service, bucket_item_service, memory_index, bucket_item_index
        )
    await db.close()


def _sources(hits: list[FusedHit]) -> list[str]:
    return [hit.source for hit in hits]


async def _insert_study_item(
    database: Database, *, memory_id: UUID, state: StudyItemState
) -> None:
    """Seed a `StudyItem` row directly, bypassing `RecallService`'s own
    generator/grader machinery — only the memory_id/state pairing matters for
    Search's human-proved derivation."""
    async with database.transaction() as tx:
        await tx.execute(
            insert(
                StudyItem(
                    memory_id=memory_id,
                    source_video_id=str(uuid7()),
                    source_title="a source",
                    state=state,
                )
            )
        )


@test()
async def fused_search_returns_hits_from_both_arms() -> None:
    """A query matching both arms returns a source-tagged, fused result."""
    h = await load_fixture(harness())
    memory = await h.memory_service.capture("aisle seat preference", logger=_logger())
    memory = await h.memory_service.tether(memory, logger=_logger())
    add_outcome = await h.bucket_item_service.add(
        "movie", {"title": "Aisle Seat"}, None, logger=_logger()
    )
    h.memory_index.search_results = [SearchCandidate(id=memory.id, score=1.0)]
    h.bucket_item_index.search_results = [
        BucketItemCandidate(id=add_outcome.item.id, score=1.0)
    ]

    hits = await h.fusion.search("aisle seat", logger=_logger())

    assert_eq(sorted(_sources(hits)), ["bucket_item", "memory"])


@test()
async def facets_filter_the_memory_arm_only() -> None:
    """A facet mismatch drops the Memory hit but leaves the Bucket-item hit."""
    h = await load_fixture(harness())
    memory = await h.memory_service.capture(
        "aisle seat preference", facets={"topic": "travel"}, logger=_logger()
    )
    memory = await h.memory_service.tether(memory, logger=_logger())
    add_outcome = await h.bucket_item_service.add(
        "movie", {"title": "Aisle Seat"}, None, logger=_logger()
    )
    h.memory_index.search_results = [SearchCandidate(id=memory.id, score=1.0)]
    h.bucket_item_index.search_results = [
        BucketItemCandidate(id=add_outcome.item.id, score=1.0)
    ]

    hits = await h.fusion.search(
        "aisle seat", facets={"topic": "cooking"}, logger=_logger()
    )

    assert_eq(_sources(hits), ["bucket_item"])


@test()
async def per_arm_sqlite_refilter_drops_a_loose_memory_orphan() -> None:
    """A drifted Memory-index entry (loose, never tethered) never surfaces."""
    h = await load_fixture(harness())
    loose = await h.memory_service.capture(
        "orphaned aisle preference", logger=_logger()
    )
    h.memory_index.search_results = [SearchCandidate(id=loose.id, score=1.0)]

    hits = await h.fusion.search("aisle", logger=_logger())

    assert_not_in(loose.id, [hit.item.id for hit in hits])


@test()
async def per_arm_sqlite_refilter_drops_a_completed_bucket_item_orphan() -> None:
    """A drifted Bucket-item-index entry (already completed) never surfaces."""
    h = await load_fixture(harness())
    add_outcome = await h.bucket_item_service.add(
        "movie", {"title": "Aisle Seat"}, None, logger=_logger()
    )
    completed = await h.bucket_item_service.complete(add_outcome.item, logger=_logger())
    h.bucket_item_index.search_results = [
        BucketItemCandidate(id=completed.id, score=1.0)
    ]

    hits = await h.fusion.search("aisle", logger=_logger())

    assert_not_in(completed.id, [hit.item.id for hit in hits])


@test()
async def sources_filter_restricts_fusion_to_the_named_arms() -> None:
    """`sources=["bucket_item"]` skips the Memory arm even when it would match."""
    h = await load_fixture(harness())
    memory = await h.memory_service.capture("aisle seat preference", logger=_logger())
    memory = await h.memory_service.tether(memory, logger=_logger())
    add_outcome = await h.bucket_item_service.add(
        "movie", {"title": "Aisle Seat"}, None, logger=_logger()
    )
    h.memory_index.search_results = [SearchCandidate(id=memory.id, score=1.0)]
    h.bucket_item_index.search_results = [
        BucketItemCandidate(id=add_outcome.item.id, score=1.0)
    ]

    hits = await h.fusion.search(
        "aisle seat", sources=["bucket_item"], logger=_logger()
    )

    assert_eq(_sources(hits), ["bucket_item"])


@test()
async def blank_query_raises_empty_search_query_error() -> None:
    """A whitespace-only query is rejected before any arm ever runs."""
    h = await load_fixture(harness())

    with assert_raises(EmptySearchQueryError):
        _ = await h.fusion.search("   ", logger=_logger())


@test()
async def a_recall_completed_memory_outranks_a_better_matching_asserted_memory() -> (
    None
):
    """A Memory behind a completed `StudyItem` outranks a manually-captured
    Memory even when the fusion engine ranked it second by raw match."""
    h = await load_fixture(harness())
    asserted = await h.memory_service.capture("asserted fact", logger=_logger())
    asserted = await h.memory_service.tether(asserted, logger=_logger())
    proved = await h.memory_service.capture("proved fact", logger=_logger())
    proved = await h.memory_service.tether(proved, logger=_logger())
    await _insert_study_item(
        h.memory_service.database, memory_id=proved.id, state="completed"
    )
    h.memory_index.search_results = [
        SearchCandidate(id=asserted.id, score=1.0),
        SearchCandidate(id=proved.id, score=0.9),
    ]

    hits = await h.fusion.search("fact", logger=_logger())

    assert_eq([hit.item.id for hit in hits], [proved.id, asserted.id])


@test()
async def a_memory_with_a_non_completed_study_item_is_not_human_proved() -> None:
    """A `studying` (not yet `completed`) `StudyItem` grants no boost — its
    Memory keeps its ordinary rank order, unaffected by trust weighting."""
    h = await load_fixture(harness())
    asserted = await h.memory_service.capture("asserted fact", logger=_logger())
    asserted = await h.memory_service.tether(asserted, logger=_logger())
    studying = await h.memory_service.capture("studying fact", logger=_logger())
    studying = await h.memory_service.tether(studying, logger=_logger())
    await _insert_study_item(
        h.memory_service.database, memory_id=studying.id, state="studying"
    )
    h.memory_index.search_results = [
        SearchCandidate(id=asserted.id, score=1.0),
        SearchCandidate(id=studying.id, score=0.9),
    ]

    hits = await h.fusion.search("fact", logger=_logger())

    assert_eq([hit.item.id for hit in hits], [asserted.id, studying.id])
