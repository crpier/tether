"""Shared transcript acquisition policy, persistence, and provider health."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from snekok.result import Err, Ok
from snekql.sqlite import Database

from tether.escalating_pause import PauseState
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptAcquisitionDeferred,
    TranscriptAcquisitionOutcome,
    TranscriptBlockedFailure,
    TranscriptDeferredFailure,
    TranscriptFetchPolicy,
    TranscriptionTarget,
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
from tether.transcripts.store import (
    TranscriptionAvailable,
    TranscriptionRetrying,
    TranscriptionReviewNeeded,
    TranscriptionStore,
    TranscriptionUnavailable,
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
        self.store: TranscriptionStore = TranscriptionStore(database)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._provider_health: TranscriptProviderHealth = TranscriptProviderHealth(
            base=self.config.block_pause_base,
            cap=self.config.block_pause_cap,
            database=database,
        )

    async def acquire(
        self,
        target: TranscriptionTarget,
        *,
        now: datetime,
        policy: TranscriptFetchPolicy | None = None,
    ) -> TranscriptAcquisitionOutcome:
        """Fetch and persist one target without knowing its source Integration."""
        async with self._lock:
            state = await self.store.read(target.key)
            if isinstance(state, TranscriptionAvailable):
                return TranscriptStored(
                    cached=True,
                    source=state.transcript.source,
                    text=state.transcript.text,
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
            outcome = await self.provider.fetch(target.locator, policy=effective_policy)
            match outcome:
                case Ok(fetched):
                    await self._store(target, fetched)
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
                    await self.store.save_review_needed(
                        target.key,
                        failed_attempts=attempts,
                        last_error=target.locator,
                    )
                    await self._publish_change()
                    return TranscriptNeedsReview(target=target)
                case Err(TranscriptTransientFailure() as failure):
                    attempts = self._failed_attempts(state) + 1
                    next_attempt_at = self._next_attempt_at(now, attempts)
                    await self.store.save_retrying(
                        target.key,
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
                    return TranscriptAcquisitionDeferred(target=target)
                case _:
                    message = "unhandled transcript source outcome"
                    raise TranscriptAcquisitionInvariantError(message)

    @staticmethod
    def _failed_attempts(
        state: TranscriptionRetrying
        | TranscriptionReviewNeeded
        | TranscriptionUnavailable
        | None,
    ) -> int:
        """Return the persisted failure count for states that carry one."""
        return state.failed_attempts if state is not None else 0

    async def _store(
        self, target: TranscriptionTarget, fetched: FetchedTranscript
    ) -> None:
        await self.store.save_available(
            target.key,
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
        await self.event_publisher.publish(InvalidateEvent(keys=["transcripts"]))
