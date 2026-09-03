"""Acquire Transcripts for eligible videos in the saved YouTube collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from snekql.sqlite import Database, Fetched, select

from tether.structured_logging import Logger
from tether.transcripts import (
    TranscriptAcquisitionDeferred,
    TranscriptAcquisitionService,
    TranscriptFetchPolicy,
    TranscriptionAvailable,
    TranscriptionRetrying,
    TranscriptionReviewNeeded,
    TranscriptionState,
    TranscriptionStore,
    TranscriptionUnavailable,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptRetryScheduled,
    TranscriptStored,
    load_all_provider_pauses,
)
from tether.youtube.quota import Clock
from tether.youtube.store import IngestedVideo
from tether.youtube.transcription import youtube_transcription_target

_LIBRARY_SOURCE = "youtube_transcript_api"


@dataclass(frozen=True, slots=True)
class TranscriptSyncConfig:
    """Work and failure-storm limits for one YouTube transcription pass."""

    library_requests_per_pass: int = 5
    recent_window: int = 50
    transient_storm_threshold: int = 8


@dataclass(frozen=True, slots=True)
class TranscriptSyncReport:
    """Outcome counts and stop conditions from one YouTube transcription pass."""

    blocked: int
    deferred: bool
    fetched: int
    needs_review: int
    paused: bool
    retried: int
    transient_storm: bool


@dataclass(slots=True)
class _TranscriptPassState:
    """Mutable counts and stop conditions for one pass."""

    blocked: int = 0
    consecutive_transient: int = 0
    deferred: bool = False
    fetched: int = 0
    needs_review: int = 0
    retried: int = 0
    transient_storm: bool = False


class TranscriptSyncService:
    """Select YouTube videos and send generic targets to Transcription."""

    def __init__(
        self,
        *,
        acquisition: TranscriptAcquisitionService,
        clock: Clock,
        database: Database,
        config: TranscriptSyncConfig | None = None,
    ) -> None:
        self.acquisition: TranscriptAcquisitionService = acquisition
        self.clock: Clock = clock
        self.config: TranscriptSyncConfig = config or TranscriptSyncConfig()
        self.database: Database = database
        self.transcriptions: TranscriptionStore = TranscriptionStore(database)

    async def sync(self, *, logger: Logger) -> TranscriptSyncReport:
        """Run one pass over the newest eligible YouTube videos."""
        logger.debug("Transcript sync starting")
        state = _TranscriptPassState()
        policy = TranscriptFetchPolicy(
            request_limits={_LIBRARY_SOURCE: self.config.library_requests_per_pass}
        )
        for video in await self._eligible(self.clock.now()):
            outcome = await self.acquisition.acquire(
                youtube_transcription_target(video.video_id),
                now=self.clock.now(),
                policy=policy,
            )
            match outcome:
                case TranscriptStored() as stored:
                    state.consecutive_transient = 0
                    if not stored.cached:
                        state.fetched += 1
                        logger.info(
                            "Transcript fetched",
                            source=stored.source,
                            video_id=video.video_id,
                        )
                case TranscriptNeedsReview():
                    state.needs_review += 1
                    state.consecutive_transient = 0
                case TranscriptProviderBlocked(source=source, paused_until=until):
                    state.blocked += 1
                    state.consecutive_transient = 0
                    logger.info(
                        "Transcript provider blocked; source paused",
                        paused_until=until.isoformat(),
                        source=source,
                    )
                case TranscriptRetryScheduled():
                    state.retried += 1
                    state.consecutive_transient += 1
                    if (
                        state.consecutive_transient
                        >= self.config.transient_storm_threshold
                    ):
                        state.transient_storm = True
                        logger.info(
                            "Transcript sync stopped: transient-failure storm",
                            consecutive_transient=state.consecutive_transient,
                            threshold=self.config.transient_storm_threshold,
                        )
                case TranscriptAcquisitionDeferred():
                    state.deferred = True
            if state.deferred or state.transient_storm:
                break

        pauses = await load_all_provider_pauses(self.database)
        now = self.clock.now()
        paused_sources = sorted(
            source for source, pause in pauses.items() if pause.is_paused(now)
        )
        logger.info(
            "Transcript sync completed",
            blocked=state.blocked,
            deferred=state.deferred,
            fetched=state.fetched,
            needs_review=state.needs_review,
            paused_sources=paused_sources,
            remaining=await self._pending_count(now),
            retried=state.retried,
            transient_storm=state.transient_storm,
        )
        return TranscriptSyncReport(
            blocked=state.blocked,
            deferred=state.deferred,
            fetched=state.fetched,
            needs_review=state.needs_review,
            paused=bool(paused_sources),
            retried=state.retried,
            transient_storm=state.transient_storm,
        )

    async def _eligible(self, now: datetime) -> list[IngestedVideo[Fetched]]:
        videos = await self._active_videos()
        states = await self.transcriptions.read_many(
            [youtube_transcription_target(video.video_id).key for video in videos]
        )
        eligible = [
            video
            for video in videos
            if self._is_eligible(
                states.get(youtube_transcription_target(video.video_id).key), now
            )
        ]
        return eligible[: self.config.recent_window]

    async def _pending_count(self, now: datetime) -> int:
        videos = await self._active_videos()
        states = await self.transcriptions.read_many(
            [youtube_transcription_target(video.video_id).key for video in videos]
        )
        return sum(
            self._is_eligible(
                states.get(youtube_transcription_target(video.video_id).key), now
            )
            for video in videos
        )

    async def _active_videos(self) -> list[IngestedVideo[Fetched]]:
        query = (
            select(IngestedVideo)
            .where(IngestedVideo.ignored_at.is_null())
            .order_by(IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc())
        )
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(query)

    @staticmethod
    def _is_eligible(state: TranscriptionState | None, now: datetime) -> bool:
        if state is None:
            return True
        if isinstance(
            state,
            TranscriptionAvailable
            | TranscriptionReviewNeeded
            | TranscriptionUnavailable,
        ):
            return False
        return (
            state.next_attempt_at <= now
            if isinstance(state, TranscriptionRetrying)
            else False
        )
