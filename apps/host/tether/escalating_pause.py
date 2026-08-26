"""A persisted, escalating cooldown ("pause") over a string key/value store.

Rate-limit style gates in the host share one shape: an upstream block pauses a
resource for a cooldown that doubles with each consecutive block (clamped to a
cap and raised to any provider-supplied retry-after hint), the pair
`(paused_until, streak)` is persisted so a standing pause survives restarts,
and the first clean call clears it. `PersistentEscalatingPause` owns that
shape once; callers configure it with their two state keys, their bounds, and
the read/write seam of whatever key/value store they persist into.

>>> import asyncio
>>> from datetime import UTC, datetime, timedelta
>>> store: dict[str, str] = {}
>>> async def read_value(key: str) -> str | None:
...     return store.get(key)
>>> async def write_value(key: str, value: str) -> None:
...     store[key] = value
>>> pause = PersistentEscalatingPause(
...     base=timedelta(minutes=15),
...     cap=timedelta(hours=6),
...     keys=PauseKeys(paused_until="api_paused_until", streak="api_block_streak"),
...     read_value=read_value,
...     write_value=write_value,
... )
>>> now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
>>> asyncio.run(pause.trip(now=now)).paused_until
datetime.datetime(2026, 3, 1, 12, 15, tzinfo=datetime.timezone.utc)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

type StateReader = Callable[[str], Awaitable[str | None]]
"""Reads one persisted string value by key; `None` and `""` both mean unset."""

type StateWriter = Callable[[str, str], Awaitable[None]]
"""Persists one string value under a key, creating or overwriting it."""


@dataclass(frozen=True, slots=True)
class PauseKeys:
    """The two store keys one pause persists its `(paused_until, streak)` under."""

    paused_until: str
    streak: str


@dataclass(frozen=True, slots=True)
class TrippedPause:
    """The state right after a trip: the pause always stands, so `paused_until`
    is a plain datetime (unlike a loaded `PauseState`, which may be open)."""

    paused_until: datetime
    streak: int

    def as_state(self) -> PauseState:
        """Repackage as a loadable `PauseState`, for pass-local pause caches."""
        return PauseState(paused_until=self.paused_until, streak=self.streak)


@dataclass(frozen=True, slots=True)
class PauseState:
    """One pause's persisted facts: when it lifts and how blocked it has been.

    `paused_until` may be `None` (cleared, or never tripped) — or sit in the
    past while `streak` stays positive: an elapsed cooldown reopens the gate
    but keeps the escalation memory until an explicit `clear`, so the next
    block escalates rather than starting over.
    """

    paused_until: datetime | None
    streak: int

    def is_paused(self, now: datetime) -> bool:
        """Whether the pause still stands at `now`."""
        return self.paused_until is not None and self.paused_until > now


class PersistentEscalatingPause:
    """A restart-surviving exponential cooldown, configured per resource.

    `trip` escalates the cooldown as `base * 2**(streak - 1)` clamped to
    `cap` and raised to any retry-after hint; `load` reads the persisted
    state back; `clear` resets it after a clean call. Every mutation is
    persisted immediately through the injected writer, so the pause holds
    across restarts.
    """

    def __init__(
        self,
        *,
        base: timedelta,
        cap: timedelta,
        keys: PauseKeys,
        read_value: StateReader,
        write_value: StateWriter,
    ) -> None:
        self._base: timedelta = base
        self._cap: timedelta = cap
        self._keys: PauseKeys = keys
        self._read_value: StateReader = read_value
        self._write_value: StateWriter = write_value

    async def load(self) -> PauseState:
        """Read the persisted `(paused_until, streak)`, defaulting to open/0."""
        return await load_pause_state(self._read_value, keys=self._keys)

    async def trip(
        self, *, now: datetime, retry_after: timedelta | None = None
    ) -> TrippedPause:
        """Escalate the pause one block, persist it, and return the new state.

        The cooldown is `base * 2**(streak - 1)` clamped to `cap`, then raised
        to any provider-supplied `retry_after` hint.
        """
        prior = await self.load()
        streak = prior.streak + 1
        cooldown = min(self._base * (2 ** (streak - 1)), self._cap)
        if retry_after is not None and retry_after > cooldown:
            cooldown = retry_after
        tripped = TrippedPause(paused_until=now + cooldown, streak=streak)
        await self._write_value(
            self._keys.paused_until, tripped.paused_until.isoformat()
        )
        await self._write_value(self._keys.streak, str(streak))
        return tripped

    async def clear(self) -> None:
        """Reset the pause and its streak after a clean call.

        Read-only in the steady state: only writes when there is a prior pause
        or streak to clear, so the common success path stays cheap.
        """
        prior = await self.load()
        if prior.paused_until is None and prior.streak == 0:
            return
        await self._write_value(self._keys.paused_until, "")
        await self._write_value(self._keys.streak, "0")


async def load_pause_state(read_value: StateReader, *, keys: PauseKeys) -> PauseState:
    """Parse one persisted `(paused_until, streak)` pair, defaulting to open/0.

    Module-level so read-only surfaces (status listings over many pauses) can
    reuse the parse without configuring a full pause's bounds.
    """
    raw_until = await read_value(keys.paused_until)
    raw_streak = await read_value(keys.streak)
    return PauseState(
        paused_until=datetime.fromisoformat(raw_until) if raw_until else None,
        streak=int(raw_streak) if raw_streak else 0,
    )
