"""Tests for the LanceDB transcript-chunk index adapter.

`TranscriptIndex` is a second hybrid retriever alongside `SearchIndex`, shaped
for transcript chunks: every row carries its parent `video_id`, and a hit
returns that id plus the chunk text as a snippet so the search path can dedupe
chunks to videos and show why each matched — without re-fetching the transcript.
These tests drive the real embedded LanceDB in a temp dir with hand-built
vectors, mirroring `test_search_index.py`.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, assert_in, assert_raises, test

from tether.transcript_index import (
    ChunkDocument,
    TranscriptIndex,
    VectorDimMismatchError,
)

_DIM = 4


def _doc(video_id: str, content: str, vector: list[float]) -> ChunkDocument:
    return ChunkDocument(id=uuid4(), video_id=video_id, content=content, vector=vector)


@test()
async def open_is_idempotent_and_persists_across_reopen() -> None:
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        kept = _doc("vid1", "android developer registration", [1.0, 0.0, 0.0, 0.0])
        index = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([kept])
        assert_eq(await index.count(), 1)

        reopened = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)

        assert_eq(await reopened.count(), 1)
        hits = await reopened.search(
            text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_in("vid1", {hit.video_id for hit in hits})


@test()
async def a_hit_carries_its_video_id_and_snippet() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        target = _doc(
            "vid1", "google forced developer signing for android", [1.0, 0.0, 0.0, 0.0]
        )
        other = _doc("vid2", "grocery shopping list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([target, other])

        hits = await index.search(text="signing", vector=[0.0, 0.0, 0.0, 1.0], limit=5)

        match = next(hit for hit in hits if hit.video_id == "vid1")
        assert_eq(match.snippet, "google forced developer signing for android")


@test()
async def multiple_chunks_can_share_one_video_id() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert(
            [
                _doc("vid1", "first chunk about android", [1.0, 0.0, 0.0, 0.0]),
                _doc("vid1", "second chunk about developers", [0.0, 1.0, 0.0, 0.0]),
            ]
        )

        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        assert_eq({hit.video_id for hit in hits}, {"vid1"})


@test()
async def a_quoted_phrase_query_does_not_crash_the_search() -> None:
    """A model-issued phrase query (quoted terms) must resolve, not error.

    LanceDB phrase queries need an FTS index built with token positions; without
    them the search raises `position is not found but required for phrase
    queries`. The index opts into positions so quoted queries are valid.
    """
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert(
            [_doc("vid1", "a terminal agent written in rust", [1.0, 0.0, 0.0, 0.0])]
        )

        hits = await index.search(
            text='"terminal agent"', vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_in("vid1", {hit.video_id for hit in hits})


@test()
async def candidates_carry_a_relevance_score() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        strong = _doc("vid1", "android android developers", [1.0, 0.0, 0.0, 0.0])
        weak = _doc("vid2", "unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([strong, weak])

        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        by_id = {hit.chunk_id: hit.score for hit in hits}
        assert_gt(by_id[strong.id], 0.0)
        assert_gt(by_id[strong.id], by_id[weak.id])


@test()
async def remove_drops_chunks_by_id() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        gone = _doc("vid1", "android note", [1.0, 0.0, 0.0, 0.0])
        kept = _doc("vid2", "grocery note", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([gone, kept])

        await index.remove([gone.id])

        assert_eq(await index.list_ids(), {kept.id})


@test()
async def list_ids_reports_every_indexed_chunk() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        assert_eq(await index.list_ids(), set())
        first = _doc("vid1", "android note", [1.0, 0.0, 0.0, 0.0])
        second = _doc("vid2", "grocery list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([first, second])

        assert_eq(await index.list_ids(), {first.id, second.id})


@test()
async def rebuild_replaces_the_whole_projection() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert([_doc("old", "stale chunk", [1.0, 0.0, 0.0, 0.0])])
        fresh = _doc("new", "fresh chunk", [0.0, 1.0, 0.0, 0.0])

        await index.rebuild([fresh])

        assert_eq(await index.list_ids(), {fresh.id})


@test()
async def reopening_with_a_different_width_is_refused() -> None:
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        index = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([_doc("vid1", "android", [1.0, 0.0, 0.0, 0.0])])
        with assert_raises(VectorDimMismatchError):
            _ = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM + 1)
