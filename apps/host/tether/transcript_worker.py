"""The background transcript sync worker, extracted from `youtube.py` (#144).

`TranscriptSyncService` is the reconciler-shaped background worker: an idempotent
`sync` pass (run at startup and on a periodic loop) that walks the newest videos
still lacking a transcript and fetches each through the shared `TranscriptProvider`
path within the daily budget, newest-liked first. It owns the transcript-only state
machine — per-source provider pause, consecutive-block escalation, the transient
storm breaker, and backed-off per-video retry — folded over one mutable
`_TranscriptPassState` per pass.

The fetch/persist path and per-video state helpers it drives
(`fetch_and_store_transcript`, `load_all_provider_pauses`, `provider_pause_keys`,
`TranscriptFetchContext`) live in `youtube.py` because the on-demand read path
(`YouTubeService.fetch_transcript`, `sync_status`) shares them; this module imports
them from there, one direction, so the two modules stay acyclic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from snekql.sqlite import Database, Fetched, select

from tether.escalating_pause import PauseState, PersistentEscalatingPause
from tether.events import EventPublisher, NullEventPublisher
from tether.logging import Logger
from tether.transcript_library import reset_library_pass_budget
from tether.youtube import (
    IngestedVideo,
    TranscriptAttempt,
    TranscriptFetchContext,
    TranscriptProvider,
    TranscriptSyncConfig,
    TranscriptSyncReport,
    YouTubeApiClient,
    YouTubeQuotaExceededError,
    YouTubeTranscriptState,
    fetch_and_store_transcript,
    load_all_provider_pauses,
    provider_pause_keys,
    state_get,
    state_set,
)

# The empty default for a video with no caption-gated skip, mirrored from youtube's
# module constant so the moved worker keeps its own local default rather than
# importing a private name across the module boundary.
_NO_PAUSED_SOURCES: frozenset[str] = frozenset()


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


@dataclass(slots=True)
class _TranscriptPassState:
    """The mutable state of one transcript sync pass: counts plus live pause state.

    Folded by `_apply_attempt` as each fetch resolves. `pauses`/`paused_sources`
    track which blockable sources are in cooldown (a fresh block escalates one and
    adds it to the set); `transient_storm` flips once a run of consecutive transient
    failures reaches the configured threshold, which is the caller's signal to end
    the pass.
    """

    pauses: dict[str, PauseState]
    paused_sources: frozenset[str]
    fetched: int = 0
    unavailable: int = 0
    excluded: int = 0
    retried: int = 0
    blocked: int = 0
    consecutive_transient: int = 0
    transient_storm: bool = False


class TranscriptSyncService:
    """Background transcript fetching, reconciler-shaped like the likes sync.

    An idempotent `sync` pass (run at startup and on a periodic loop) walks the
    newest videos still lacking a transcript — skipping ones whose per-video state
    is terminal or whose backed-off retry is not yet due — and fetches each through
    the shared `TranscriptProvider` path within the daily budget, newest-liked
    first. It stops for the day the moment the budget is exhausted, resuming next
    pass. The per-video state machine (`YouTubeTranscriptState`) makes retries and
    terminal classifications durable across restarts.
    """

    def __init__(
        self,
        database: Database,
        client: YouTubeApiClient,
        provider: TranscriptProvider,
        *,
        config: TranscriptSyncConfig | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.client: YouTubeApiClient = client
        self.provider: TranscriptProvider = provider
        self.config: TranscriptSyncConfig = config or TranscriptSyncConfig()
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    @property
    def _context(self) -> TranscriptFetchContext:
        return TranscriptFetchContext(
            database=self.database,
            provider=self.provider,
            config=self.config,
            event_publisher=self.event_publisher,
        )

    async def sync(self, *, logger: Logger) -> TranscriptSyncReport:
        """Run one pass: fetch transcripts for eligible videos within budget.

        Honors each blockable source's persisted pause independently: while a
        source (the free library, Supadata) is in its cooldown the pass still runs
        the reachable sources (captions, plus any other unpaused fallback) but tells
        the provider to skip the paused one. A fresh block trips (or escalates) that
        source's own pause; a clean fetch while a source was reachable clears that
        source's streak.
        """
        quota_exhausted = False
        _debug(logger, "Transcript sync starting")
        # Refill the library provider's per-pass request budget (and clear its
        # "blocked this pass" latch) before walking candidates — it is a
        # long-lived object shared across passes, so without this reset a budget
        # spent (or a block observed) last pass would stay exhausted/latched
        # forever instead of just for that one pass (issue #179).
        reset_library_pass_budget(self.provider)
        context = self._context
        pauses = await load_all_provider_pauses(self.database)
        state = _TranscriptPassState(
            pauses=pauses,
            paused_sources=self._paused_sources(pauses, self.client.now()),
        )
        for source in state.paused_sources:
            pause = pauses[source]
            _info(
                logger,
                "Transcript provider paused; skipping source",
                source=source,
                paused_until=pause.paused_until.isoformat()
                if pause.paused_until is not None
                else None,
                streak=pause.streak,
            )
        candidates = await self._eligible(self.client.now())
        for video in candidates:
            try:
                await self.client.charge_transcript()
            except YouTubeQuotaExceededError as error:
                quota_exhausted = True
                _debug(logger, "Transcript sync stopped on quota", error=str(error))
                break
            now = self.client.now()
            attempt = await fetch_and_store_transcript(
                context,
                video_id=video.video_id,
                now=now,
                paused_sources=state.paused_sources,
                skip_sources=self._skip_sources_for(video),
            )
            await self._apply_attempt(
                state, video=video, attempt=attempt, now=now, logger=logger
            )
            if state.transient_storm:
                # A systematic failure tripped the storm breaker — stop before
                # spending a call/credit on the rest of the window. The next
                # scheduled pass retries, so a real transient still recovers.
                break
        paused = self._paused_sources(state.pauses, self.client.now())
        remaining = await self._pending_count(self.client.now())
        _info(
            logger,
            "Transcript sync completed",
            fetched=state.fetched,
            unavailable=state.unavailable,
            excluded=state.excluded,
            retried=state.retried,
            blocked=state.blocked,
            paused=bool(paused),
            paused_sources=sorted(paused),
            quota_exhausted=quota_exhausted,
            transient_storm=state.transient_storm,
            remaining=remaining,
        )
        return TranscriptSyncReport(
            fetched=state.fetched,
            unavailable=state.unavailable,
            excluded=state.excluded,
            retried=state.retried,
            quota_exhausted=quota_exhausted,
            blocked=state.blocked,
            paused=bool(paused),
            transient_storm=state.transient_storm,
        )

    async def _apply_attempt(
        self,
        state: _TranscriptPassState,
        *,
        video: IngestedVideo[Fetched],
        attempt: TranscriptAttempt,
        now: datetime,
        logger: Logger,
    ) -> None:
        """Fold one fetch outcome into `state` (counts and live pause state).

        A fresh block escalates that source's own pause and adds it to
        `state.paused_sources`; a run of consecutive transient failures flips
        `state.transient_storm` (the caller ends the pass on it). Any non-transient
        outcome resets that run, since the chain answered meaningfully.
        """
        if attempt.outcome != "transient":
            state.consecutive_transient = 0
        if attempt.outcome == "done":
            state.fetched += 1
            # Name the source that produced it so transcript provenance (and which
            # paid/free source was billed) is visible in the logs.
            _info(
                logger,
                "Transcript fetched",
                video_id=video.video_id,
                source=attempt.source,
            )
            # A clean fetch means every reachable source was healthy this pass, so
            # reset the escalation streak of each unpaused blocked source.
            await self._reset_reachable_streaks(state.pauses, state.paused_sources)
        elif attempt.outcome == "unavailable":
            state.unavailable += 1
        elif attempt.outcome == "excluded":
            state.excluded += 1
        elif attempt.outcome == "blocked":
            state.blocked += 1
            source = attempt.source
            if source is not None and source not in state.paused_sources:
                state.pauses[source] = await self._trip_pause(
                    source, now, attempt.retry_after, logger=logger
                )
                state.paused_sources = state.paused_sources | {source}
            # else: a deferral (composite skipped an already-paused fallback) or an
            # already-paused source, so the video stays pending — nothing to do.
        else:
            state.retried += 1
            state.consecutive_transient += 1
            if state.consecutive_transient >= self.config.transient_storm_threshold:
                state.transient_storm = True
                _info(
                    logger,
                    "Transcript sync stopped: transient-failure storm",
                    consecutive_transient=state.consecutive_transient,
                    threshold=self.config.transient_storm_threshold,
                )

    def _skip_sources_for(self, video: IngestedVideo[Fetched]) -> frozenset[str]:
        """Sources to drop for this video: the caption-gated ones when it has no
        manual captions (`caption_available` is stored as 0)."""
        if video.caption_available == 0:
            return self.config.caption_gated_sources
        return _NO_PAUSED_SOURCES

    @staticmethod
    def _paused_sources(
        pauses: Mapping[str, PauseState], now: datetime
    ) -> frozenset[str]:
        """The set of sources still in their cooldown at `now`."""
        return frozenset(
            source for source, pause in pauses.items() if pause.is_paused(now)
        )

    async def _reset_reachable_streaks(
        self,
        pauses: dict[str, PauseState],
        paused_sources: frozenset[str],
    ) -> None:
        """Clear the streak of each unpaused source that had one (its block cleared)."""
        for source, pause in list(pauses.items()):
            if source not in paused_sources and pause.streak > 0:
                await self._provider_pause(source).clear()
                pauses[source] = PauseState(paused_until=None, streak=0)

    async def _trip_pause(
        self,
        source: str,
        now: datetime,
        retry_after: timedelta | None,
        *,
        logger: Logger,
    ) -> PauseState:
        """Escalate a source's pause one block, persist it, and return the new state."""
        tripped = await self._provider_pause(source).trip(
            now=now, retry_after=retry_after
        )
        _info(
            logger,
            "Transcript provider blocked; pausing source",
            source=source,
            streak=tripped.streak,
            cooldown_seconds=(tripped.paused_until - now).total_seconds(),
            paused_until=tripped.paused_until.isoformat(),
            retry_after_seconds=retry_after.total_seconds()
            if retry_after is not None
            else None,
        )
        return tripped.as_state()

    def _provider_pause(self, source: str) -> PersistentEscalatingPause:
        """One blockable source's persisted pause, bounded by the worker config."""
        return PersistentEscalatingPause(
            base=self.config.block_pause_base,
            cap=self.config.block_pause_cap,
            keys=provider_pause_keys(source),
            read_value=partial(state_get, self.database),
            write_value=partial(state_set, self.database),
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run transcript sync passes on the given interval until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Transcript sync pass failed")

    def _eligible_query(self, now: datetime):  # noqa: ANN202 (snekql query type is internal)
        """Build the select for videos eligible for a transcript fetch.

        Active, still-untranscribed videos whose state is neither terminal nor a
        not-yet-due retry, newest-liked first. Terminal and not-due rows are
        excluded in SQL (not just sliced out in Python) so the recent window never
        fills with permanently-failed videos and starves the backlog.
        """
        blocked = select(YouTubeTranscriptState.video_id).where(
            YouTubeTranscriptState.status.eq("terminal")
            | (
                YouTubeTranscriptState.status.eq("retry")
                & YouTubeTranscriptState.next_attempt_at.gt(now.isoformat())
            )
        )
        return (
            select(IngestedVideo)
            .where(IngestedVideo.transcript.is_null())
            .where(IngestedVideo.ignored_at.is_null())
            .where(IngestedVideo.video_id.not_in_subquery(blocked))
        )

    async def _eligible(self, now: datetime) -> list[IngestedVideo[Fetched]]:
        query = (
            self._eligible_query(now)
            .order_by(IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc())
            .limit(self.config.recent_window)
        )
        async with self.database.transaction() as tx:
            return await tx.fetch_all(query)

    async def _pending_count(self, now: datetime) -> int:
        """Count active videos still owed a transcript (excluding terminal)."""
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(self._eligible_query(now))
        return len(rows)
