"""The transcript search read path: query -> ranked video matches with snippets.

`TranscriptSearchService` is the single collaborator `YouTubeService` talks to
for semantic transcript search. It embeds the query, runs hybrid chunk retrieval
through `TranscriptIndex`, and folds the chunk hits down to one match per video —
keeping each video's best-scoring chunk as the snippet that explains the match.

It over-fetches chunks (a single video can own several of the top hits), so the
caller still gets `limit` distinct videos after dedup. Reached through a narrow
`TranscriptQueryPort`, so the service is testable against a fake index with no
LanceDB.

>>> service = TranscriptSearchService(embedder=embedder, index=index)
>>> [m.video_id for m in await service.candidates("android signing", limit=10, logger=logger)]
[]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tether.embeddings import Embedder
    from tether.logging import Logger
    from tether.transcript_index import ChunkCandidate

# A single video can own several of the top chunk hits, so fetch this many chunks
# per requested video to keep enough distinct videos after dedup.
_DEFAULT_OVERFETCH = 5


@dataclass(frozen=True, slots=True)
class VideoMatch:
    """One video the query matched: its id, the best snippet, and its score."""

    video_id: str
    snippet: str
    score: float


class TranscriptQueryPort(Protocol):
    """The read slice of `TranscriptIndex` the search path needs."""

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[ChunkCandidate]: ...


class TranscriptSearchService:
    """Embeds the query, retrieves chunks, and dedupes them to ranked videos."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        index: TranscriptQueryPort,
        overfetch: int = _DEFAULT_OVERFETCH,
    ) -> None:
        self.embedder: Embedder = embedder
        self.index: TranscriptQueryPort = index
        self.overfetch: int = overfetch

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[VideoMatch]:
        """Return up to `limit` videos ranked by their best matching chunk.

        An empty/whitespace query short-circuits to no matches without touching
        the model or the index."""
        if not query.strip():
            return []
        logger.debug(
            "Embedding transcript search query", query_length=len(query), limit=limit
        )
        vector = await self.embedder.embed_query(query)
        chunk_limit = max(limit * self.overfetch, limit)
        hits = await self.index.search(text=query, vector=vector, limit=chunk_limit)

        best: dict[str, VideoMatch] = {}
        for hit in hits:
            current = best.get(hit.video_id)
            if current is None or hit.score > current.score:
                best[hit.video_id] = VideoMatch(
                    video_id=hit.video_id, snippet=hit.snippet, score=hit.score
                )
        ranked = sorted(best.values(), key=lambda match: match.score, reverse=True)
        return ranked[:limit]
