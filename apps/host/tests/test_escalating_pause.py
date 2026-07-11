"""Focused unit tests for the persistent escalating-pause primitive.

The escalation and retry-after math is proven once here, against a dict-backed
in-memory store; the YouTube api-gate and transcript-worker suites keep only
their wiring coverage (that a live quota error / provider block reaches the
pause and that the pause gates their calls).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from snektest import assert_eq, test

from tether.escalating_pause import PauseKeys, PersistentEscalatingPause

_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

_BASE = timedelta(minutes=15)
"""Base cooldown every test's ladder starts from."""

_CAP = timedelta(hours=6)
"""Clamp the escalation stops doubling at."""


def dict_backed_pause(
    store: dict[str, str],
    *,
    base: timedelta = _BASE,
    cap: timedelta = _CAP,
) -> PersistentEscalatingPause:
    """A pause persisting into a plain dict, standing in for the sync-state table."""

    async def read_value(key: str) -> str | None:
        return store.get(key)

    async def write_value(key: str, value: str) -> None:
        store[key] = value

    return PersistentEscalatingPause(
        base=base,
        cap=cap,
        keys=PauseKeys(paused_until="paused_until", streak="streak"),
        read_value=read_value,
        write_value=write_value,
    )


@test()
async def a_fresh_pause_is_open_with_no_streak() -> None:
    """With nothing persisted, the pause is open and the streak is zero."""
    pause = dict_backed_pause({})

    state = await pause.load()

    assert_eq(state.is_paused(_NOW), False)
    assert_eq(state.streak, 0)


@test()
async def a_trip_pauses_for_the_base_interval() -> None:
    """The first trip stands a pause of exactly one base interval."""
    pause = dict_backed_pause({})

    tripped = await pause.trip(now=_NOW)

    assert_eq(tripped.paused_until, _NOW + timedelta(minutes=15))
    assert_eq(tripped.streak, 1)
    reloaded = await pause.load()
    assert_eq(reloaded.is_paused(_NOW + timedelta(minutes=14)), True)
    assert_eq(reloaded.is_paused(_NOW + timedelta(minutes=16)), False)


@test()
async def consecutive_trips_escalate_exponentially_up_to_the_cap() -> None:
    """Each consecutive trip doubles the cooldown, clamped to the cap."""
    pause = dict_backed_pause({})

    cooldowns = [(await pause.trip(now=_NOW)).paused_until - _NOW for _ in range(7)]

    assert_eq(
        cooldowns,
        [
            timedelta(minutes=15),
            timedelta(minutes=30),
            timedelta(minutes=60),
            timedelta(minutes=120),
            timedelta(minutes=240),
            timedelta(hours=6),
            timedelta(hours=6),
        ],
    )


@test()
async def a_retry_after_hint_floors_the_cooldown() -> None:
    """A retry-after hint longer than the computed cooldown wins."""
    pause = dict_backed_pause({})

    tripped = await pause.trip(now=_NOW, retry_after=timedelta(hours=2))

    assert_eq(tripped.paused_until, _NOW + timedelta(hours=2))


@test()
async def a_retry_after_hint_shorter_than_the_cooldown_is_ignored() -> None:
    """The escalated cooldown stands when the hint would shorten it."""
    pause = dict_backed_pause({})

    tripped = await pause.trip(now=_NOW, retry_after=timedelta(minutes=1))

    assert_eq(tripped.paused_until, _NOW + timedelta(minutes=15))


@test()
async def an_elapsed_pause_keeps_the_streak_so_the_next_trip_escalates() -> None:
    """Waiting out a cooldown reopens the pause but keeps the escalation memory."""
    pause = dict_backed_pause({})
    _ = await pause.trip(now=_NOW)

    after_cooldown = _NOW + timedelta(minutes=20)
    assert_eq((await pause.load()).is_paused(after_cooldown), False)
    second = await pause.trip(now=after_cooldown)

    assert_eq(second.streak, 2)
    assert_eq(second.paused_until, after_cooldown + timedelta(minutes=30))


@test()
async def clear_resets_the_pause_and_the_streak() -> None:
    """After a clear, the pause is open and the next trip starts from the base."""
    pause = dict_backed_pause({})
    _ = await pause.trip(now=_NOW)
    _ = await pause.trip(now=_NOW)

    await pause.clear()

    cleared = await pause.load()
    assert_eq(cleared.is_paused(_NOW), False)
    assert_eq(cleared.streak, 0)
    assert_eq((await pause.trip(now=_NOW)).paused_until, _NOW + timedelta(minutes=15))


@test()
async def a_standing_pause_survives_a_fresh_instance_over_the_same_store() -> None:
    """The pause is persisted, so a restart (new instance) still sees it."""
    store: dict[str, str] = {}
    _ = await dict_backed_pause(store).trip(now=_NOW)

    reopened = await dict_backed_pause(store).load()

    assert_eq(reopened.is_paused(_NOW + timedelta(minutes=10)), True)
    assert_eq(reopened.streak, 1)
