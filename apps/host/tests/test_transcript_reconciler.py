"""Behavior tests for the transcript-chunk reconciler.

`TranscriptReconciler` is the sole writer that converges the transcript-chunk
LanceDB index with SQLite (the canonical `ingested_video` rows). It re-derives
chunks from each active transcript, embeds only the chunk ids absent from the
index, and drops orphans — so a fresh index embeds everything, an ignored video
falls out, and a re-run is a no-op. These run against a real in-memory SQLite
database with a `FakeTranscriptIndex` and a `CountingEmbedder`, no model download.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from snekql.sqlite import Config, CurrentTimestamp, Database, insert, update
from snektest import assert_eq, fixture, load_fixture, test

from tether.embeddings import Embedder, FakeEmbedder, Vector
from tether.logging import Logger
from tether.transcript_index import ChunkDocument
from tether.transcript_reconciler import TranscriptReconciler
from tether.youtube import IngestedVideo, create_youtube_schema

_DIM = 16


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.transcript_reconciler")


class FakeTranscriptIndex:
    """In-memory stand-in for `TranscriptIndex`; records every write."""

    def __init__(self) -> None:
        self.docs: dict[UUID, ChunkDocument] = {}
        self.optimize_calls: int = 0

    async def upsert(self, documents: Sequence[ChunkDocument]) -> None:
        for document in documents:
            self.docs[document.id] = document

    async def remove(self, ids: Sequence[UUID]) -> None:
        for identifier in ids:
            _ = self.docs.pop(identifier, None)

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
    reconciler: TranscriptReconciler
    database: Database
    index: FakeTranscriptIndex
    embedder: CountingEmbedder


@fixture
async def harness() -> AsyncGenerator[Harness]:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    embedder = CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-a"))
    index = FakeTranscriptIndex()
    reconciler = TranscriptReconciler(
        database=db, index=index, embedder=embedder, chunk_max_chars=40
    )
    yield Harness(reconciler, db, index, embedder)
    await db.close()


async def _add_video(
    db: Database,
    video_id: str,
    *,
    transcript: str | None,
    ignored: bool = False,
    title: str = "title",
) -> None:
    async with db.transaction() as tx:
        created = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=video_id,
                    source="liked",
                    title=title,
                    channel="chan",
                    topic="topic",
                    description="desc",
                    transcript=transcript,
                )
            ).returning()
        )
        if ignored:
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .where(IngestedVideo.id.eq(created.id))
            )


@test()
async def fresh_index_embeds_and_indexes_every_chunk() -> None:
    h = await load_fixture(harness())
    long_text = " ".join(f"w{index}" for index in range(60))
    await _add_video(h.database, "vid1", transcript=long_text)

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(len(h.index.docs) > 1, True)
    assert_eq(report.indexed, len(h.index.docs))
    assert_eq(report.embedded, len(h.index.docs))
    assert_eq(h.embedder.documents_embedded, len(h.index.docs))
    assert_eq({doc.video_id for doc in h.index.docs.values()}, {"vid1"})


@test()
async def videos_without_a_transcript_are_still_indexed_by_their_header() -> None:
    h = await load_fixture(harness())
    await _add_video(h.database, "vid1", transcript=None, title="android forever")

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.indexed > 0, True)
    assert_eq({doc.video_id for doc in h.index.docs.values()}, {"vid1"})


@test()
async def a_video_with_no_searchable_text_is_skipped() -> None:
    h = await load_fixture(harness())
    async with h.database.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="vid1",
                    source="liked",
                    title="",
                    channel="chan",
                    topic="topic",
                    description="",
                    transcript=None,
                )
            ).returning()
        )

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.indexed, 0)
    assert_eq(h.index.docs, {})


@test()
async def a_second_pass_re_embeds_nothing() -> None:
    h = await load_fixture(harness())
    text = " ".join(f"w{index}" for index in range(60))
    await _add_video(h.database, "vid1", transcript=text)
    _ = await h.reconciler.reconcile(logger=_logger())
    embedded_after_first = h.embedder.documents_embedded

    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(h.embedder.documents_embedded, embedded_after_first)
    assert_eq(report.embedded, 0)
    assert_eq(report.removed, 0)


@test()
async def ignoring_a_video_drops_its_chunks_as_orphans() -> None:
    h = await load_fixture(harness())
    text = " ".join(f"w{index}" for index in range(60))
    await _add_video(h.database, "vid1", transcript=text)
    _ = await h.reconciler.reconcile(logger=_logger())
    indexed_count = len(h.index.docs)

    async with h.database.transaction() as tx:
        _ = await tx.execute(
            update(IngestedVideo)
            .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
            .where(IngestedVideo.video_id.eq("vid1"))
        )
    report = await h.reconciler.reconcile(logger=_logger())

    assert_eq(report.removed, indexed_count)
    assert_eq(h.index.docs, {})


@test()
async def a_model_swap_re_embeds_the_whole_corpus() -> None:
    h = await load_fixture(harness())
    text = " ".join(f"w{index}" for index in range(60))
    await _add_video(h.database, "vid1", transcript=text)
    _ = await h.reconciler.reconcile(logger=_logger())
    original_ids = set(h.index.docs)

    swapped = TranscriptReconciler(
        database=h.database,
        index=h.index,
        embedder=CountingEmbedder(FakeEmbedder(vector_dim=_DIM, model_name="fake-b")),
        chunk_max_chars=40,
    )
    report = await swapped.reconcile(logger=_logger())

    assert_eq(report.embedded, len(h.index.docs))
    assert_eq(report.removed, len(original_ids))
    assert_eq(set(h.index.docs).isdisjoint(original_ids), True)
