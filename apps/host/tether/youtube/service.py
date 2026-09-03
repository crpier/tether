"""User-facing operations over the locally ingested YouTube corpus."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, Self

from opentelemetry.trace import Tracer
from snekql.sqlite import (
    Database,
    Fetched,
    Transaction,
    select,
)

from tether.capability_contracts import CacheMeta
from tether.structured_logging import Logger
from tether.youtube.quota import QuotaMeta, YouTubeApiClient
from tether.youtube.store import IngestedVideo, YouTubeSource
from tether.youtube.sync import read_last_youtube_sync_at
from tether.youtube.transcription_service import (
    YouTubeTranscriptionService,
    YouTubeTranscriptionSummary,
    YouTubeVideoNotFoundError,
)
from tether.youtube.types import VideoId

if TYPE_CHECKING:
    from tether.youtube.search import VideoMatch

_DEFAULT_SEMANTIC_LIMIT = 50


def _empty_snippets() -> dict[str, str]:
    """Typed empty default for `SearchResult.snippets` (the lexical path)."""
    return {}


class EmptyYouTubeSearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class InvalidYouTubeActivityRangeError(Exception):
    """Raised when a liked-video activity interval is ambiguous or empty."""

    @classmethod
    def timezone_required(cls) -> Self:
        """Construct the failure for an ambiguous naive boundary."""
        return cls("after and before must include timezone offsets")

    @classmethod
    def reversed_or_empty(cls) -> Self:
        """Construct the failure for a non-forward interval."""
        return cls("before must be later than after")


class YouTubeSearchPort(Protocol):
    """Rank transcript-bearing videos for `YouTubeService.search`."""

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> Sequence[VideoMatch]:
        """Return ranked video matches for one query."""
        ...


@dataclass(frozen=True, slots=True)
class BrowseResult:
    """A topic-filtered browse: the local videos plus the day's quota/cache."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta


@dataclass(frozen=True, slots=True)
class YouTubeActivitySummary:
    """A bounded liked-video proxy over one half-open UTC interval.

    Duration is the sum of full video lengths, not measured playback time.
    Explicit coverage prevents a partial total from masquerading as complete.
    """

    after: datetime
    before: datetime
    video_count: int
    videos_with_known_duration: int
    videos_missing_duration: int
    total_video_duration_seconds: int
    average_video_duration_seconds: int | None
    cache: CacheMeta
    quota: QuotaMeta


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A video search with an optional matching Transcript excerpt per result."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta
    snippets: dict[str, str] = field(default_factory=_empty_snippets)


@dataclass(frozen=True, slots=True)
class YouTubeSyncStatus:
    """YouTube ingestion health plus a separate Transcription summary."""

    videos_total: int
    last_synced_at: datetime | None
    quota: QuotaMeta
    api_paused_until: datetime | None
    transcriptions: YouTubeTranscriptionSummary


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


@dataclass
class YouTubeService:
    """Capability surface for the local YouTube ingested corpus.

    Browse, Search, and activity summaries read local video metadata. Search
    composes with the separate YouTube Transcription adapter for transcript text.
    """

    database: Database
    client: YouTubeApiClient
    tracer: Tracer
    transcriptions: YouTubeTranscriptionService
    youtube_search: YouTubeSearchPort | None = None

    async def summarize_activity(
        self,
        *,
        after: datetime,
        before: datetime,
        logger: Logger,
    ) -> YouTubeActivitySummary:
        """Summarize active liked videos in the half-open interval `[after, before)`.

        Timestamps must be timezone-aware. They are normalized to UTC before the
        SQLite query so caller offsets define calendar boundaries without
        changing the canonical storage representation.
        """
        if (
            after.tzinfo is None
            or after.utcoffset() is None
            or before.tzinfo is None
            or before.utcoffset() is None
        ):
            raise InvalidYouTubeActivityRangeError.timezone_required()
        if after >= before:
            raise InvalidYouTubeActivityRangeError.reversed_or_empty()
        after_utc = after.astimezone(UTC)
        before_utc = before.astimezone(UTC)
        with self.tracer.start_as_current_span("YouTubeService.summarize_activity"):
            async with self.database.transaction() as tx:
                videos = await tx.fetch_all(
                    select(IngestedVideo)
                    .where(IngestedVideo.source.eq("liked"))
                    .where(IngestedVideo.ignored_at.is_null())
                    .where(IngestedVideo.liked_at.gte(after_utc))
                    .where(IngestedVideo.liked_at.lt(before_utc))
                )
        durations = [
            video.duration_seconds
            for video in videos
            if video.duration_seconds is not None
        ]
        total_duration = sum(durations)
        average_duration = round(total_duration / len(durations)) if durations else None
        _debug(logger, "YouTube activity summarized", result_count=len(videos))
        return YouTubeActivitySummary(
            after=after_utc,
            before=before_utc,
            video_count=len(videos),
            videos_with_known_duration=len(durations),
            videos_missing_duration=len(videos) - len(durations),
            total_video_duration_seconds=total_duration,
            average_video_duration_seconds=average_duration,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def browse(
        self,
        *,
        topic: str | None = None,
        source: YouTubeSource | None = None,
        limit: int | None = None,
        logger: Logger,
    ) -> BrowseResult:
        """List active ingested videos from the local corpus, newest-liked-first.

        Reads only local state — the background sync is what refreshes the
        corpus, so a browse never calls upstream and costs no quota. `limit`
        caps the rows returned (`None` is unbounded); assistant-facing callers
        pass a bound so a large corpus can't flood the model's context.
        """
        with self.tracer.start_as_current_span("YouTubeService.browse"):
            _debug(logger, "Browsing YouTube ingestion", topic=topic, source=source)
            query = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
            if source is not None:
                query = query.where(IngestedVideo.source.eq(source))
            if topic is not None:
                query = query.where(IngestedVideo.topic.like(topic))
            query = query.order_by(
                IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
            )
            if limit is not None:
                query = query.limit(limit)
            async with self.database.transaction() as tx:
                videos = await tx.fetch_all(query)
        _debug(logger, "YouTube browse completed", result_count=len(videos))
        return BrowseResult(
            videos=videos,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def sync_status(self, *, logger: Logger) -> YouTubeSyncStatus:
        """Summarise the background ingestion's progress and health (local only).

        Reads the local corpus and bookkeeping — never upstream — so the UI can
        poll it cheaply: how many videos are ingested, the transcript backlog,
        when the likes sync last ran, the day's quota, and any active pause (a
        live Data API block or a per-source transcript provider block) that
        explains why progress has stalled.
        """
        with self.tracer.start_as_current_span("YouTubeService.sync_status"):
            now = self.client.now()
            async with self.database.transaction() as tx:
                active = await tx.fetch_all(
                    select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
                )
            transcription_summary = await self.transcriptions.summary(active)
            total = len(active)
            last_run = await read_last_youtube_sync_at(self.database)
            api_paused_until = await self.client.api_paused_until(now=now)
        status = YouTubeSyncStatus(
            videos_total=total,
            last_synced_at=last_run,
            quota=await self.client.snapshot(),
            api_paused_until=api_paused_until,
            transcriptions=transcription_summary,
        )
        _debug(
            logger,
            "YouTube sync status computed",
            videos_total=total,
            transcripts_pending=transcription_summary.pending,
        )
        return status

    async def search(
        self, query: str, *, limit: int | None = None, logger: Logger
    ) -> SearchResult:
        """Search saved content and transcript text (local only).

        When semantic transcript search is wired (`youtube_search`), the query
        is embedded and matched against the transcript-chunk index, ranking videos
        by relevance and returning the best-matching snippet per video. With
        search disabled it falls back to the lexical SQLite `LIKE` path: each
        whitespace term matched case-insensitively against title, description, or
        transcript and AND-ed. Only active videos match; `limit` caps the rows.
        """
        if not query.split():
            message = "keyword Search requires a non-empty query"
            raise EmptyYouTubeSearchQueryError(message)
        if self.youtube_search is not None:
            return await self._semantic_search(query, limit=limit, logger=logger)
        return await self._lexical_search(query, limit=limit, logger=logger)

    async def _semantic_search(
        self, query: str, *, limit: int | None, logger: Logger
    ) -> SearchResult:
        """Embed the query, rank videos by transcript relevance, attach snippets."""
        assert self.youtube_search is not None
        video_limit = limit if limit is not None else _DEFAULT_SEMANTIC_LIMIT
        _debug(logger, "Searching YouTube transcripts semantically", limit=video_limit)
        matches = await self.youtube_search.candidates(
            query, limit=video_limit, logger=logger
        )
        if not matches:
            _debug(logger, "YouTube semantic search completed", result_count=0)
            return SearchResult(
                videos=[],
                cache=CacheMeta(hit=True, source="cache"),
                quota=await self.client.snapshot(),
            )
        video_ids = [VideoId(match.video_id) for match in matches]
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(
                select(IngestedVideo)
                .where(IngestedVideo.video_id.in_(*video_ids))
                .where(IngestedVideo.ignored_at.is_null())
            )
        by_video_id = {video.video_id: video for video in videos}
        # Preserve relevance order and drop any match whose video has since been
        # ignored or deleted (index drift the next reconcile would clean up).
        ordered = [
            by_video_id[VideoId(match.video_id)]
            for match in matches
            if VideoId(match.video_id) in by_video_id
        ]
        snippets = {
            match.video_id: match.snippet
            for match in matches
            if VideoId(match.video_id) in by_video_id
        }
        _debug(logger, "YouTube semantic search completed", result_count=len(ordered))
        return SearchResult(
            videos=ordered,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
            snippets=snippets,
        )

    async def _lexical_search(
        self, query: str, *, limit: int | None, logger: Logger
    ) -> SearchResult:
        """The SQLite `LIKE` fallback used when semantic search is disabled."""
        terms = [term.casefold() for term in query.split()]
        _debug(logger, "Searching YouTube ingestion", terms_count=len(terms))
        statement = (
            select(IngestedVideo)
            .where(IngestedVideo.ignored_at.is_null())
            .order_by(IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc())
        )
        async with self.database.transaction() as tx:
            candidates = await tx.fetch_all(statement)
        transcripts = await self.transcriptions.texts_for(candidates)
        videos = [
            video
            for video in candidates
            if all(
                term
                in "\n".join(
                    (
                        video.title,
                        video.description,
                        transcripts.get(video.video_id, ""),
                    )
                ).casefold()
                for term in terms
            )
        ]
        if limit is not None:
            videos = videos[:limit]
        _debug(logger, "YouTube search completed", result_count=len(videos))
        return SearchResult(
            videos=videos,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def get_video(self, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch one ingested video by its upstream id, or raise when absent."""
        async with self.database.transaction() as tx:
            return await self._fetch(tx, video_id)

    async def _fetch(self, tx: Transaction, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch an ingested video by its upstream id or raise."""
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(VideoId(video_id)))
        )
        if video is None:
            raise YouTubeVideoNotFoundError(video_id)
        return video
