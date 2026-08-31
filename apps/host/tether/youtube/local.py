"""Deterministic in-process adapter for YouTube and transcript boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from snekok.result import Err, Ok

from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptFetchResult,
    TranscriptUnavailableFailure,
)
from tether.youtube.quota import LikedPage, RawYouTubeVideo, YouTubeApi


class InMemoryYouTubeApi(YouTubeApi):
    """A seedable in-memory YouTube API and transcript-source test double.

    Seeded with an ordered liked list (newest first), it serves fixed-size pages
    with synthetic cursors and counts its calls so tests can prove ingestion
    stays within budget. Metadata omits `liked_at`, matching the live API.

    >>> import asyncio
    >>> api = InMemoryYouTubeApi(transcripts={"v1": "hello"})
    >>> asyncio.run(api.fetch("v1")).unwrap().text
    'hello'
    """

    def __init__(
        self,
        *,
        liked: Sequence[RawYouTubeVideo] = (),
        transcripts: Mapping[str, str] | None = None,
        unavailable: Sequence[str] = (),
    ) -> None:
        self._liked: list[RawYouTubeVideo] = list(liked)
        unavailable_ids = set(unavailable)
        self._by_id: dict[str, RawYouTubeVideo] = {
            video.video_id: video.model_copy(update={"liked_at": None})
            for video in self._liked
            if video.video_id not in unavailable_ids
        }
        self._transcripts: dict[str, str] = dict(transcripts or {})
        self.list_calls: int = 0
        self.metadata_calls: int = 0
        self.transcript_calls: int = 0

    source: str = "in_memory"

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Return one deterministic page from the seeded liked list."""
        self.list_calls += 1
        start = int(page_token) if page_token is not None else 0
        size = max(1, page_size)
        page = self._liked[start : start + size]
        next_start = start + size
        next_token = str(next_start) if next_start < len(self._liked) else None
        return LikedPage(
            videos=list(page),
            next_page_token=next_token,
            total_results=len(self._liked),
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Return seeded metadata for known, available videos."""
        self.metadata_calls += 1
        return {
            video_id: self._by_id[video_id]
            for video_id in video_ids
            if video_id in self._by_id
        }

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        """Return a seeded transcript or typed unavailability."""
        self.transcript_calls += 1
        text = self._transcripts.get(video_id)
        if text is None:
            return Err(TranscriptUnavailableFailure(video_id=video_id))
        return Ok(FetchedTranscript(text=text, source="in_memory"))
