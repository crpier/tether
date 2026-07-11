"""Projection tests for the TranscriptIndex over HybridLanceTable.

`TranscriptIndex` is a thin chunk-shaped projection of `HybridLanceTable`
(`ChunkDocument` in, `ChunkCandidate` out; a `video_id` payload column and the
chunk text surfaced as a snippet). The generic retrieval, lifecycle, and
salvage behaviors are proven once in `test_hybrid_lance_table.py`; these tests
pin only what the projection adds — its candidate mapping.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, test

from tether.transcript_index import ChunkDocument, TranscriptIndex

_DIM = 4


@test()
async def a_hit_carries_its_video_id_and_snippet() -> None:
    """A search hit surfaces as a `ChunkCandidate`: the chunk id, its parent
    video, the chunk text as a snippet, and the RRF score — so the search path
    can dedupe chunks to videos and show why each matched."""
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        chunk = ChunkDocument(
            id=uuid4(),
            video_id="vid1",
            content="google forced developer signing for android",
            vector=[1.0, 0.0, 0.0, 0.0],
        )
        await index.upsert([chunk])

        hits = await index.search(text="signing", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        assert_eq(hits[0].chunk_id, chunk.id)
        assert_eq(hits[0].video_id, "vid1")
        assert_eq(hits[0].snippet, "google forced developer signing for android")
        assert_gt(hits[0].score, 0.0)


@test()
async def multiple_chunks_can_share_one_video_id() -> None:
    """Chunk rows are keyed by chunk id, so one video maps to many rows."""
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert(
            [
                ChunkDocument(
                    id=uuid4(),
                    video_id="vid1",
                    content="first chunk about android",
                    vector=[1.0, 0.0, 0.0, 0.0],
                ),
                ChunkDocument(
                    id=uuid4(),
                    video_id="vid1",
                    content="second chunk about developers",
                    vector=[0.0, 1.0, 0.0, 0.0],
                ),
            ]
        )

        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        assert_eq({hit.video_id for hit in hits}, {"vid1"})
