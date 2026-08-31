"""Shared transcript acquisition policy, persistence, and provider health."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from snekok.result import Err, Ok
from snekql.sqlite import Database, Fetched, select

from tether.escalating_pause import PauseState
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptAcquisitionDeferred,
    TranscriptAcquisitionOutcome,
    TranscriptBlockedFailure,
    TranscriptDeferredFailure,
    TranscriptFetchPolicy,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptProviderChain,
    TranscriptRetryScheduled,
    TranscriptStored,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)
from tether.transcripts.provider_health import (
    TranscriptProviderHealth,
    load_all_provider_pauses,
)
from tether.youtube import (
    IngestedVideo,
    TranscriptAvailable,
    TranscriptRetrying,
    TranscriptReviewNeeded,
    TranscriptUnavailable,
    VideoId,
    YouTubeVideoNotFoundError,
    fetch_transcript_state,
    write_transcript_available,
    write_transcript_retrying,
    write_transcript_review_needed,
)


@dataclass(frozen=True, slots=True)
class TranscriptAcquisitionConfig:
    """Retry and provider-pause policy shared by every acquisition caller."""

    backoff_base: timedelta = timedelta(minutes=10)
    backoff_cap: timedelta = timedelta(hours=6)
    block_pause_base: timedelta = timedelta(hours=2)
    block_pause_cap: timedelta = timedelta(hours=24)


class TranscriptAcquisitionInvariantError(Exception):
    """Raised when an exhaustive acquisition result is violated."""


class TranscriptAcquisitionService:
    """Serialize transcript fetches and apply one persistence policy everywhere."""

    def __init__(
        self,
        *,
        database: Database,
        provider: TranscriptProviderChain,
        config: TranscriptAcquisitionConfig | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.provider: TranscriptProviderChain = provider
        self.config: TranscriptAcquisitionConfig = (
            config or TranscriptAcquisitionConfig()
        )
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._provider_health: TranscriptProviderHealth = TranscriptProviderHealth(
            base=self.config.block_pause_base,
            cap=self.config.block_pause_cap,
            database=database,
        )

    async def acquire(
        self,
        video_id: str,
        *,
        now: datetime,
        policy: TranscriptFetchPolicy | None = None,
    ) -> TranscriptAcquisitionOutcome:
        """Fetch and persist one transcript without duplicate concurrent calls."""
        async with self._lock:
            video = await self._video(video_id)
            async with self.database.transaction() as tx:
                state = await fetch_transcript_state(tx, video.id)
            if isinstance(state, TranscriptAvailable):
                return TranscriptStored(
                    cached=True,
                    source=state.source,
                    text=state.text,
                )
            pauses = await load_all_provider_pauses(self.database)
            deferred_sources = frozenset(
                source for source, pause in pauses.items() if pause.is_paused(now)
            )
            selected_policy = policy or TranscriptFetchPolicy()
            effective_policy = TranscriptFetchPolicy(
                attempts=selected_policy.attempts,
                deferred_sources=(selected_policy.deferred_sources | deferred_sources),
                excluded_sources=selected_policy.excluded_sources,
                request_limits=selected_policy.request_limits,
            )
            outcome = await self.provider.fetch(video_id, policy=effective_policy)
            match outcome:
                case Ok(fetched):
                    await self._store(video_id, fetched)
                    await self._clear_reachable_streaks(
                        pauses, effective_policy.deferred_sources
                    )
                    await self._publish_change()
                    return TranscriptStored(
                        cached=False,
                        source=fetched.source,
                        text=fetched.text,
                    )
                case Err(TranscriptUnavailableFailure()):
                    attempts = max(1, self._failed_attempts(state))
                    async with self.database.transaction(mode="immediate") as tx:
                        await write_transcript_review_needed(
                            tx,
                            video.id,
                            failed_attempts=attempts,
                            last_error=video_id,
                        )
                    await self._publish_change()
                    return TranscriptNeedsReview(video_id=video_id)
                case Err(TranscriptTransientFailure() as failure):
                    attempts = self._failed_attempts(state) + 1
                    next_attempt_at = self._next_attempt_at(now, attempts)
                    async with self.database.transaction(mode="immediate") as tx:
                        await write_transcript_retrying(
                            tx,
                            video.id,
                            failed_attempts=attempts,
                            last_error=str(failure),
                            next_attempt_at=next_attempt_at,
                        )
                    return TranscriptRetryScheduled(next_attempt_at=next_attempt_at)
                case Err(TranscriptBlockedFailure() as failure):
                    paused = await self._provider_health.trip(
                        failure.source,
                        now=now,
                        retry_after=failure.retry_after,
                    )
                    return TranscriptProviderBlocked(
                        paused_until=paused.paused_until,
                        source=failure.source,
                    )
                case Err(TranscriptDeferredFailure()):
                    return TranscriptAcquisitionDeferred(video_id=video_id)
                case _:
                    message = "unhandled transcript source outcome"
                    raise TranscriptAcquisitionInvariantError(message)

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

    @staticmethod
    def _failed_attempts(
        state: TranscriptRetrying
        | TranscriptReviewNeeded
        | TranscriptUnavailable
        | None,
    ) -> int:
        """Return the persisted failure count for states that carry one."""
        return state.failed_attempts if state is not None else 0

    async def _store(self, video_id: str, fetched: FetchedTranscript) -> None:
        video = await self._video(video_id)
        async with self.database.transaction(mode="immediate") as tx:
            await write_transcript_available(
                tx,
                video.id,
                source=fetched.source,
                text=fetched.text,
                segments=fetched.segments,
            )

    def _next_attempt_at(self, now: datetime, attempts: int) -> datetime:
        exponent = max(0, attempts - 1)
        delay = cast(
            "timedelta",
            min(self.config.backoff_base * (2**exponent), self.config.backoff_cap),
        )
        return now + delay

    async def _clear_reachable_streaks(
        self,
        pauses: dict[str, PauseState],
        deferred_sources: frozenset[str],
    ) -> None:
        await self._provider_health.clear_reachable(pauses, deferred_sources)

    async def _publish_change(self) -> None:
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
