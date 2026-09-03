"""YouTube adapter for source-independent Transcript acquisition and storage."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from opentelemetry.trace import Tracer
from snekok.result import Err, Ok, Result
from snekql.sqlite import Database, Fetched, select

from tether.capability_contracts import CacheMeta
from tether.escalating_pause import PauseState
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.structured_logging import Logger
from tether.transcripts import (
    TranscriptAcquisitionDeferred,
    TranscriptAcquisitionPort,
    TranscriptExplicitlyUnavailable,
    TranscriptionAvailable,
    TranscriptionReviewNeeded,
    TranscriptionStatus,
    TranscriptionStore,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptRequestFailure,
    TranscriptRetryScheduled,
    TranscriptStored,
)
from tether.youtube.quota import Clock
from tether.youtube.store import IngestedVideo
from tether.youtube.transcription import youtube_transcription_target
from tether.youtube.types import VideoId


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


class ProviderPausesPort(Protocol):
    """Load persisted transcript-provider health for status reporting."""

    async def __call__(self, database: Database) -> Mapping[str, PauseState]: ...


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """Transcript text returned through the YouTube association."""

    transcript: str
    cache: CacheMeta


type TranscriptRequestResult = Result[TranscriptResult, TranscriptRequestFailure]


@dataclass(frozen=True, slots=True)
class TranscriptDecision:
    """A provider-exhausted video awaiting a human Transcription decision."""

    video: IngestedVideo[Fetched]
    last_error: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class TranscriptDecisionOutcome:
    """The status produced by a human Transcription decision."""

    video_id: str
    transcript_status: Literal["pending", "unavailable"]


@dataclass(frozen=True, slots=True)
class TranscriptProviderPause:
    """A transcript source currently inside its block cooldown."""

    source: str
    paused_until: datetime


@dataclass(frozen=True, slots=True)
class YouTubeTranscriptionSummary:
    """Transcription progress for a set of associated YouTube videos."""

    done: int
    pending: int
    needs_review: int
    unavailable: int
    providers_paused: list[TranscriptProviderPause]


@dataclass
class YouTubeTranscriptionService:
    """Associate YouTube videos with source-independent Transcriptions."""

    database: Database
    clock: Clock
    tracer: Tracer
    acquisition: TranscriptAcquisitionPort | None = None
    event_publisher: EventPublisher = field(default_factory=NullEventPublisher)
    provider_pauses: ProviderPausesPort | None = None
    store: TranscriptionStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = TranscriptionStore(self.database)

    async def status(self, video_id: str) -> TranscriptionStatus:
        """Read the Transcription status associated with one YouTube video."""
        video = await self._video(video_id)
        state = await self.store.read(youtube_transcription_target(video.video_id).key)
        return state.status if state is not None else "pending"

    async def stored_text(self, video_id: str) -> str | None:
        """Return associated Transcript text without starting acquisition."""
        video = await self._video(video_id)
        state = await self.store.read(youtube_transcription_target(video.video_id).key)
        return (
            state.transcript.text if isinstance(state, TranscriptionAvailable) else None
        )

    async def texts_for(
        self, videos: Sequence[IngestedVideo[Fetched]]
    ) -> dict[VideoId, str]:
        """Return completed Transcript text keyed by associated YouTube ID."""
        targets = {
            video.video_id: youtube_transcription_target(video.video_id)
            for video in videos
        }
        states = await self.store.read_many([target.key for target in targets.values()])
        return {
            video_id: state.transcript.text
            for video_id, target in targets.items()
            if isinstance(state := states.get(target.key), TranscriptionAvailable)
        }

    async def decisions(self, *, logger: Logger) -> list[TranscriptDecision]:
        """List active videos whose Transcriptions need a human decision."""
        with self.tracer.start_as_current_span("YouTubeTranscriptionService.decisions"):
            async with self.database.transaction() as tx:
                videos = await tx.fetch_all(
                    select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
                )
            targets = {
                video.video_id: youtube_transcription_target(video.video_id)
                for video in videos
            }
            states = await self.store.read_many(
                [target.key for target in targets.values()]
            )
            review_items = [
                (video, state)
                for video in videos
                if isinstance(
                    state := states.get(targets[video.video_id].key),
                    TranscriptionReviewNeeded,
                )
            ]
            review_items.sort(key=lambda item: item[1].updated_at, reverse=True)
            decisions = [
                TranscriptDecision(
                    video=video,
                    last_error=state.last_error,
                    attempts=state.failed_attempts,
                )
                for video, state in review_items
            ]
        logger.debug("Listed transcript decisions", result_count=len(decisions))
        return decisions

    async def keep_trying(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptDecisionOutcome:
        """Return a review-needed Transcription to pending acquisition."""
        video = await self._video(video_id)
        target = youtube_transcription_target(video.video_id)
        state = await self.store.read(target.key)
        if not isinstance(state, TranscriptionReviewNeeded):
            raise TranscriptUnavailableError(video_id)
        await self.store.restart(target.key)
        await self.event_publisher.publish(InvalidateEvent(keys=["transcripts"]))
        logger.info("Transcript acquisition re-opened", video_id=video_id)
        return TranscriptDecisionOutcome(video_id=video_id, transcript_status="pending")

    async def give_up(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptDecisionOutcome:
        """Settle a review-needed Transcription as unavailable."""
        video = await self._video(video_id)
        target = youtube_transcription_target(video.video_id)
        state = await self.store.read(target.key)
        if not isinstance(state, TranscriptionReviewNeeded):
            raise TranscriptUnavailableError(video_id)
        await self.store.save_unavailable(
            target.key,
            failed_attempts=state.failed_attempts,
            last_error=state.last_error,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["transcripts"]))
        logger.info("Transcript marked unavailable", video_id=video_id)
        return TranscriptDecisionOutcome(
            video_id=video_id, transcript_status="unavailable"
        )

    async def summary(
        self,
        videos: Sequence[IngestedVideo[Fetched]],
    ) -> YouTubeTranscriptionSummary:
        """Summarize Transcription states and provider pauses for videos."""
        targets = {
            video.video_id: youtube_transcription_target(video.video_id)
            for video in videos
        }
        states = await self.store.read_many([target.key for target in targets.values()])
        counts: dict[TranscriptionStatus, int] = {
            "pending": 0,
            "retrying": 0,
            "needs_review": 0,
            "available": 0,
            "unavailable": 0,
        }
        for target in targets.values():
            state = states.get(target.key)
            counts[state.status if state is not None else "pending"] += 1
        now = self.clock.now()
        pauses: Mapping[str, PauseState] = (
            await self.provider_pauses(self.database)
            if self.provider_pauses is not None
            else dict[str, PauseState]()
        )
        providers_paused = [
            TranscriptProviderPause(source=source, paused_until=pause.paused_until)
            for source, pause in sorted(pauses.items())
            if pause.is_paused(now) and pause.paused_until is not None
        ]
        return YouTubeTranscriptionSummary(
            done=counts["available"],
            pending=counts["pending"] + counts["retrying"],
            needs_review=counts["needs_review"],
            unavailable=counts["unavailable"],
            providers_paused=providers_paused,
        )

    async def fetch(self, video_id: str, *, logger: Logger) -> TranscriptRequestResult:
        """Return a stored Transcript or a typed expected request failure."""
        logger.debug("Fetching YouTube transcript", video_id=video_id)
        video = await self._video(video_id)
        target = youtube_transcription_target(video.video_id)
        state = await self.store.read(target.key)
        if isinstance(state, TranscriptionReviewNeeded):
            return Err(TranscriptNeedsReview(target=target))
        if state is not None and state.status == "unavailable":
            return Err(TranscriptExplicitlyUnavailable(target=target))
        if isinstance(state, TranscriptionAvailable):
            return Ok(
                TranscriptResult(
                    transcript=state.transcript.text,
                    cache=CacheMeta(hit=True, source="cache"),
                )
            )
        if self.acquisition is None:
            return Err(TranscriptNeedsReview(target=target))
        outcome = await self.acquisition.acquire(target, now=self.clock.now())
        match outcome:
            case TranscriptStored(text=text):
                logger.info("YouTube transcript fetched", video_id=video_id)
                return Ok(
                    TranscriptResult(
                        transcript=text,
                        cache=CacheMeta(hit=False, source="live"),
                    )
                )
            case (
                TranscriptNeedsReview()
                | TranscriptProviderBlocked()
                | TranscriptRetryScheduled()
                | TranscriptAcquisitionDeferred()
            ) as failure:
                return Err(failure)

    async def _video(self, video_id: str) -> IngestedVideo[Fetched]:
        async with self.database.transaction() as tx:
            video = await tx.fetch_one_or_none(
                select(IngestedVideo).where(
                    IngestedVideo.video_id.eq(VideoId(video_id))
                )
            )
        if video is None:
            raise YouTubeVideoNotFoundError(video_id)
        return video
