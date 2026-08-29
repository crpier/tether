"""User-facing operations over the locally ingested YouTube corpus."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol, Self

from opentelemetry.trace import Tracer
from snekok.result import Err, Ok, Result
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    delete,
    select,
    update,
)

from tether.capability_contracts import CacheMeta
from tether.escalating_pause import PauseState
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.structured_logging import Logger
from tether.transcripts.contracts import (
    TranscriptAcquisitionDeferred,
    TranscriptAcquisitionPort,
    TranscriptExplicitlyUnavailable,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptRequestFailure,
    TranscriptRetryScheduled,
    TranscriptStored,
)
from tether.youtube.quota import QuotaMeta, YouTubeApiClient
from tether.youtube.store import (
    IngestedVideo,
    TranscriptStateWrite,
    TranscriptStatus,
    YouTubeSource,
    YouTubeTranscriptState,
    derive_transcript_status,
    fetch_transcript_state,
    fetch_transcript_statuses,
    write_transcript_state,
)
from tether.youtube.sync import read_last_youtube_sync_at

if TYPE_CHECKING:
    from tether.youtube.search import VideoMatch

_DEFAULT_SEMANTIC_LIMIT = 50


def _empty_snippets() -> dict[str, str]:
    """Typed empty default for `SearchResult.snippets` (the lexical path)."""
    return {}


class YouTubeVideoNotFoundError(Exception):
    """Raised when an operation targets a video absent from ingestion."""


class TranscriptUnavailableError(Exception):
    """Raised when an application request targets unavailable transcript data."""


class TranscriptNeedsReviewError(TranscriptUnavailableError):
    """Raised when provider exhaustion requires a human transcript decision."""


class TranscriptTransientError(Exception):
    """Raised when an on-demand transcript request fails transiently."""


class TranscriptBlockedError(Exception):
    """Raised when an on-demand transcript request is provider-blocked."""

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: timedelta | None = None,
        source: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after: timedelta | None = retry_after
        self.source: str | None = source


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


class ProviderPausesPort(Protocol):
    """Loads persisted provider-health pause state for status reporting.

    Satisfied by `tether.transcripts.provider_health.load_all_provider_pauses`;
    injected so this module never imports the transcripts worker, which reads
    YouTube sync state through this package's interface (ADR-0025).
    """

    async def __call__(self, database: Database) -> Mapping[str, PauseState]: ...


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
    transcript_statuses: Mapping[str, TranscriptStatus]
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
    """A search across saved content + transcripts, with the day's quota/cache.

    `snippets` maps a matched video's `video_id` to the transcript excerpt that
    explains the match; it is populated by the semantic path and empty for the
    lexical fallback."""

    videos: list[IngestedVideo[Fetched]]
    transcript_statuses: Mapping[str, TranscriptStatus]
    cache: CacheMeta
    quota: QuotaMeta
    snippets: dict[str, str] = field(default_factory=_empty_snippets)


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """A fetched transcript, the updated video row, and quota/cache."""

    video: IngestedVideo[Fetched]
    transcript: str
    cache: CacheMeta
    quota: QuotaMeta


type TranscriptRequestResult = Result[TranscriptResult, TranscriptRequestFailure]


@dataclass(frozen=True, slots=True)
class TranscriptDecision:
    """A provider-exhausted video awaiting the human's transcript decision."""

    video: IngestedVideo[Fetched]
    last_error: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class TranscriptDecisionOutcome:
    """The normalized status produced by a human transcript decision."""

    video_id: str
    transcript_status: Literal["pending", "unavailable"]


@dataclass(frozen=True, slots=True)
class TranscriptProviderPause:
    """A blockable transcript source currently inside its IP-block cooldown."""

    source: str
    paused_until: datetime


@dataclass(frozen=True, slots=True)
class YouTubeSyncStatus:
    """A snapshot of the background ingestion's progress and health.

    The transcript counts partition the active corpus: every active video is
    transcribed, still pending/retrying, awaiting a human decision, or explicitly
    unavailable; their sum is `videos_total`. `last_synced_at`
    is when the likes sync last ran, `quota` the day's YouTube Data API budget
    (only liked-list and metadata calls count against it), and the two pause fields
    explain a stall
    (a live Data API block, or a per-source transcript provider block).
    """

    videos_total: int
    transcripts_done: int
    transcripts_pending: int
    transcripts_needs_review: int
    transcripts_unavailable: int
    last_synced_at: datetime | None
    quota: QuotaMeta
    api_paused_until: datetime | None
    transcript_providers_paused: list[TranscriptProviderPause]


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


@dataclass
class YouTubeService:
    """Capability surface for the local YouTube ingested corpus.

    Browse, Search, and activity summaries read only `IngestedVideo` (instant,
    no quota). Transcript requests delegate to the shared acquisition service
    and short-circuit once text is stored. Each mutation owns its transaction.
    """

    database: Database
    client: YouTubeApiClient
    tracer: Tracer
    acquisition: TranscriptAcquisitionPort | None = None
    event_publisher: EventPublisher = field(default_factory=NullEventPublisher)
    youtube_search: YouTubeSearchPort | None = None
    provider_pauses: ProviderPausesPort | None = None

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
                transcript_statuses = await fetch_transcript_statuses(tx, videos)
        _debug(logger, "YouTube browse completed", result_count=len(videos))
        return BrowseResult(
            videos=videos,
            transcript_statuses=transcript_statuses,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def transcript_status(self, video_id: str) -> TranscriptStatus:
        """Read one video's normalized transcript status from local state."""
        async with self.database.transaction() as tx:
            video = await self._fetch(tx, video_id)
            state = await fetch_transcript_state(tx, video_id)
        return derive_transcript_status(video, state)

    async def transcript_decisions(self, *, logger: Logger) -> list[TranscriptDecision]:
        """List active videos whose providers are exhausted, newest decision first."""
        with self.tracer.start_as_current_span("YouTubeService.transcript_decisions"):
            async with self.database.transaction() as tx:
                states = await tx.fetch_all(
                    select(YouTubeTranscriptState)
                    .where(YouTubeTranscriptState.status.eq("needs_review"))
                    .order_by(YouTubeTranscriptState.updated_at.desc())
                )
                if not states:
                    return []
                videos = await tx.fetch_all(
                    select(IngestedVideo)
                    .where(
                        IngestedVideo.video_id.in_(
                            *(state.video_id for state in states)
                        )
                    )
                    .where(IngestedVideo.ignored_at.is_null())
                )
            video_by_id = {video.video_id: video for video in videos}
            decisions = [
                TranscriptDecision(
                    video=video_by_id[state.video_id],
                    last_error=state.last_error,
                    attempts=state.attempts,
                )
                for state in states
                if state.video_id in video_by_id
            ]
        _debug(logger, "Listed transcript decisions", result_count=len(decisions))
        return decisions

    async def keep_trying_transcript(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptDecisionOutcome:
        """Return a review-needed transcript to the background acquisition queue."""

        async def _keep_trying(tx: Transaction) -> None:
            _ = await self._fetch(tx, video_id)
            state = await fetch_transcript_state(tx, video_id)
            if state is None or state.status != "needs_review":
                raise TranscriptUnavailableError(video_id)
            _ = await tx.execute(
                delete(YouTubeTranscriptState).where(
                    YouTubeTranscriptState.video_id.eq(video_id)
                )
            )

        async with self.database.transaction(mode="immediate") as tx:
            await _keep_trying(tx)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        _info(logger, "Transcript acquisition re-opened", video_id=video_id)
        return TranscriptDecisionOutcome(video_id=video_id, transcript_status="pending")

    async def give_up_transcript(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptDecisionOutcome:
        """Settle a review-needed transcript as explicitly unavailable."""

        async def _give_up(tx: Transaction) -> None:
            _ = await self._fetch(tx, video_id)
            state = await fetch_transcript_state(tx, video_id)
            if state is None or state.status != "needs_review":
                raise TranscriptUnavailableError(video_id)
            await write_transcript_state(
                tx,
                video_id,
                TranscriptStateWrite(
                    status="unavailable",
                    attempts=state.attempts,
                    next_attempt_at=None,
                    last_error=state.last_error,
                ),
            )

        async with self.database.transaction(mode="immediate") as tx:
            await _give_up(tx)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        _info(logger, "Transcript marked unavailable", video_id=video_id)
        return TranscriptDecisionOutcome(
            video_id=video_id, transcript_status="unavailable"
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
                states = await tx.fetch_all(select(YouTubeTranscriptState).all())
            state_by_video_id = {state.video_id: state.status for state in states}
            done_count = sum(video.transcript is not None for video in active)
            needs_review_count = sum(
                video.transcript is None
                and state_by_video_id.get(video.video_id) == "needs_review"
                for video in active
            )
            unavailable_count = sum(
                video.transcript is None
                and state_by_video_id.get(video.video_id) == "unavailable"
                for video in active
            )
            total = len(active)
            pending_count = total - done_count - needs_review_count - unavailable_count
            last_run = await read_last_youtube_sync_at(self.database)
            api_paused_until = await self.client.api_paused_until(now=now)
            pauses: Mapping[str, PauseState] = (
                await self.provider_pauses(self.database)
                if self.provider_pauses is not None
                else {}
            )
        providers_paused = [
            TranscriptProviderPause(source=source, paused_until=pause.paused_until)
            for source, pause in sorted(pauses.items())
            if pause.is_paused(now) and pause.paused_until is not None
        ]
        status = YouTubeSyncStatus(
            videos_total=total,
            transcripts_done=done_count,
            transcripts_pending=pending_count,
            transcripts_needs_review=needs_review_count,
            transcripts_unavailable=unavailable_count,
            last_synced_at=last_run,
            quota=await self.client.snapshot(),
            api_paused_until=api_paused_until,
            transcript_providers_paused=providers_paused,
        )
        _debug(
            logger,
            "YouTube sync status computed",
            videos_total=total,
            transcripts_pending=pending_count,
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
                transcript_statuses={},
                cache=CacheMeta(hit=True, source="cache"),
                quota=await self.client.snapshot(),
            )
        video_ids = [match.video_id for match in matches]
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(
                select(IngestedVideo)
                .where(IngestedVideo.video_id.in_(*video_ids))
                .where(IngestedVideo.ignored_at.is_null())
            )
            transcript_statuses = await fetch_transcript_statuses(tx, videos)
        by_video_id = {video.video_id: video for video in videos}
        # Preserve relevance order and drop any match whose video has since been
        # ignored or deleted (index drift the next reconcile would clean up).
        ordered = [
            by_video_id[match.video_id]
            for match in matches
            if match.video_id in by_video_id
        ]
        snippets = {
            match.video_id: match.snippet
            for match in matches
            if match.video_id in by_video_id
        }
        _debug(logger, "YouTube semantic search completed", result_count=len(ordered))
        return SearchResult(
            videos=ordered,
            transcript_statuses=transcript_statuses,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
            snippets=snippets,
        )

    async def _lexical_search(
        self, query: str, *, limit: int | None, logger: Logger
    ) -> SearchResult:
        """The SQLite `LIKE` fallback used when semantic search is disabled."""
        terms = query.split()
        _debug(logger, "Searching YouTube ingestion", terms_count=len(terms))
        statement = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
        for term in terms:
            pattern = f"%{term}%"
            statement = statement.where(
                IngestedVideo.title.like(pattern)
                | IngestedVideo.description.like(pattern)
                | IngestedVideo.transcript.like(pattern)
            )
        statement = statement.order_by(
            IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(statement)
            transcript_statuses = await fetch_transcript_statuses(tx, videos)
        _debug(logger, "YouTube search completed", result_count=len(videos))
        return SearchResult(
            videos=videos,
            transcript_statuses=transcript_statuses,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def fetch_transcript(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptRequestResult:
        """Return a stored transcript or a typed expected request failure."""
        _debug(logger, "Fetching YouTube transcript", video_id=video_id)
        video = await self.get_video(video_id)
        transcript_status = await self.transcript_status(video_id)
        request_result: TranscriptRequestResult
        if transcript_status == "needs_review":
            request_result = Err(TranscriptNeedsReview(video_id=video_id))
        elif transcript_status == "unavailable":
            request_result = Err(TranscriptExplicitlyUnavailable(video_id=video_id))
        elif video.transcript is not None:
            request_result = Ok(
                TranscriptResult(
                    video=video,
                    transcript=video.transcript,
                    cache=CacheMeta(hit=True, source="cache"),
                    quota=await self.client.snapshot(),
                )
            )
        elif self.acquisition is None:
            request_result = Err(TranscriptNeedsReview(video_id=video_id))
        else:
            outcome = await self.acquisition.acquire(video_id, now=self.client.now())
            match outcome:
                case TranscriptStored(text=text):
                    _info(logger, "YouTube transcript fetched", video_id=video_id)
                    request_result = Ok(
                        TranscriptResult(
                            video=await self.get_video(video_id),
                            transcript=text,
                            cache=CacheMeta(hit=False, source="live"),
                            quota=await self.client.snapshot(),
                        )
                    )
                case (
                    TranscriptNeedsReview()
                    | TranscriptProviderBlocked()
                    | TranscriptRetryScheduled()
                    | TranscriptAcquisitionDeferred()
                ) as failure:
                    request_result = Err(failure)
        return request_result

    async def ignore(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Purge a video from ingestion so browse/search no longer surface it."""
        _debug(logger, "Ignoring YouTube video", video_id=video_id)

        async def _ignore(tx: Transaction) -> IngestedVideo[Fetched]:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
                .where(IngestedVideo.ignored_at.is_null())
            )
            return await self._fetch(tx, video_id)

        async with self.database.transaction(mode="immediate") as tx:
            video = await _ignore(tx)
        _info(logger, "YouTube video ignored", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return video

    async def retry(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Un-ignore a previously purged video, returning it to ingestion."""
        _debug(logger, "Retrying YouTube video", video_id=video_id)

        async def _retry(tx: Transaction) -> IngestedVideo[Fetched]:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(None))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
            )
            return await self._fetch(tx, video_id)

        async with self.database.transaction(mode="immediate") as tx:
            video = await _retry(tx)
        _info(logger, "YouTube video retried", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return video

    async def get_video(self, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch one ingested video by its upstream id, or raise when absent."""
        async with self.database.transaction() as tx:
            return await self._fetch(tx, video_id)

    async def _fetch(self, tx: Transaction, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch an ingested video by its upstream id or raise."""
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )
        if video is None:
            raise YouTubeVideoNotFoundError(video_id)
        return video
