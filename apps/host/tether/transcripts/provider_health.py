"""Persisted provider pauses shared by acquisition and status reporting."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from functools import partial

from snekql.sqlite import Database, select

from tether.escalating_pause import (
    PauseKeys,
    PauseState,
    PersistentEscalatingPause,
    TrippedPause,
    load_pause_state,
)
from tether.youtube import YouTubeSyncState, state_get, state_set

_TRANSCRIPT_PAUSED_UNTIL_PREFIX = "transcript_provider_paused_until:"
_TRANSCRIPT_BLOCK_STREAK_PREFIX = "transcript_provider_block_streak:"


def provider_pause_keys(source: str) -> PauseKeys:
    """Return persisted provider-health keys for one source."""
    return PauseKeys(
        paused_until=f"{_TRANSCRIPT_PAUSED_UNTIL_PREFIX}{source}",
        streak=f"{_TRANSCRIPT_BLOCK_STREAK_PREFIX}{source}",
    )


async def load_all_provider_pauses(database: Database) -> dict[str, PauseState]:
    """Load every source with persisted provider-health state."""
    async with database.transaction() as tx:
        until_rows = await tx.fetch_all(
            select(YouTubeSyncState).where(
                YouTubeSyncState.key.like(f"{_TRANSCRIPT_PAUSED_UNTIL_PREFIX}%")
            )
        )
        streak_rows = await tx.fetch_all(
            select(YouTubeSyncState).where(
                YouTubeSyncState.key.like(f"{_TRANSCRIPT_BLOCK_STREAK_PREFIX}%")
            )
        )
    sources = {
        row.key.removeprefix(_TRANSCRIPT_PAUSED_UNTIL_PREFIX) for row in until_rows
    } | {row.key.removeprefix(_TRANSCRIPT_BLOCK_STREAK_PREFIX) for row in streak_rows}
    pauses: dict[str, PauseState] = {}
    for source in sources:
        pauses[source] = await load_pause_state(
            partial(state_get, database), keys=provider_pause_keys(source)
        )
    return pauses


class TranscriptProviderHealth:
    """Read and mutate escalating pauses for transcript sources."""

    def __init__(
        self,
        *,
        base: timedelta,
        cap: timedelta,
        database: Database,
    ) -> None:
        self.base: timedelta = base
        self.cap: timedelta = cap
        self.database: Database = database

    async def trip(
        self,
        source: str,
        *,
        now: datetime,
        retry_after: timedelta | None,
    ) -> TrippedPause:
        """Escalate and persist one source's pause."""
        return await self._pause(source).trip(now=now, retry_after=retry_after)

    async def clear_reachable(
        self,
        pauses: Mapping[str, PauseState],
        deferred_sources: frozenset[str],
    ) -> None:
        """Clear prior block streaks for sources reached by a successful fetch."""
        for source, pause in pauses.items():
            if source not in deferred_sources and pause.streak > 0:
                await self._pause(source).clear()

    def _pause(self, source: str) -> PersistentEscalatingPause:
        return PersistentEscalatingPause(
            base=self.base,
            cap=self.cap,
            keys=provider_pause_keys(source),
            read_value=partial(state_get, self.database),
            write_value=partial(state_set, self.database),
        )
