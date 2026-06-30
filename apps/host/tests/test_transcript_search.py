"""Tests for the transcript search read path.

`TranscriptSearchService` embeds the query, runs hybrid chunk search, and folds
the chunk hits down to one ranked match per video — keeping each video's
best-scoring chunk as the snippet. It over-fetches chunks so dedup still yields
enough distinct videos. Driven by a fake index returning canned chunk
candidates and a `FakeEmbedder`; no model download, no LanceDB.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import structlog
from snektest import assert_eq, test

from tether.embeddings import FakeEmbedder
from tether.logging import Logger
from tether.transcript_index import ChunkCandidate
from tether.transcript_search import TranscriptSearchService


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.transcript_search")


class FakeIndex:
    """Returns a fixed list of chunk candidates, recording the requested limit."""

    def __init__(self, candidates: list[ChunkCandidate]) -> None:
        self._candidates: list[ChunkCandidate] = candidates
        self.requested_limit: int | None = None

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[ChunkCandidate]:
        _ = (text, vector)
        self.requested_limit = limit
        return self._candidates[:limit]


def _chunk(video_id: str, snippet: str, score: float) -> ChunkCandidate:
    return ChunkCandidate(
        chunk_id=uuid4(), video_id=video_id, snippet=snippet, score=score
    )


@test()
async def folds_chunks_to_one_ranked_match_per_video() -> None:
    index = FakeIndex(
        [
            _chunk("vid1", "android signing chunk", 0.9),
            _chunk("vid2", "grocery chunk", 0.5),
            _chunk("vid1", "another android chunk", 0.3),
        ]
    )
    service = TranscriptSearchService(embedder=FakeEmbedder(), index=index)

    matches = await service.candidates("android", limit=10, logger=_logger())

    assert_eq([match.video_id for match in matches], ["vid1", "vid2"])
    assert_eq(matches[0].snippet, "android signing chunk")
    assert_eq(matches[0].score, 0.9)


@test()
async def caps_results_at_the_video_limit() -> None:
    index = FakeIndex(
        [_chunk(f"vid{n}", f"snippet {n}", 1.0 - n / 100) for n in range(20)]
    )
    service = TranscriptSearchService(embedder=FakeEmbedder(), index=index)

    matches = await service.candidates("query", limit=3, logger=_logger())

    assert_eq(len(matches), 3)


@test()
async def over_fetches_chunks_to_survive_dedup() -> None:
    index = FakeIndex([_chunk("vid1", "snippet", 0.9)])
    service = TranscriptSearchService(embedder=FakeEmbedder(), index=index, overfetch=5)

    _ = await service.candidates("query", limit=4, logger=_logger())

    assert_eq(index.requested_limit, 20)


@test()
async def an_empty_query_yields_no_matches() -> None:
    index = FakeIndex([_chunk("vid1", "snippet", 0.9)])
    service = TranscriptSearchService(embedder=FakeEmbedder(), index=index)

    matches = await service.candidates("   ", limit=10, logger=_logger())

    assert_eq(matches, [])
    assert_eq(index.requested_limit, None)
