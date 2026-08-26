"""Validated Scheduled trigger definitions and wall-clock calculations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

type TriggerActionKind = Literal["message", "prompt"]
"""What a fired trigger does: deliver fixed text, or run a prompt through pi."""

type TriggerRecurrence = Literal["once", "daily", "weekly"]
"""How often a trigger fires: a single instant, or a wall-clock recurrence."""

_MAX_WEEKDAY = 6
"""Highest valid weekday index (Monday is 0, Sunday is 6)."""


class InvalidTriggerSpecError(Exception):
    """Raised when a trigger definition is invalid."""


@dataclass(frozen=True, slots=True)
class OnceTriggerSpec:
    """A trigger definition scheduled for one absolute instant.

    ```python
    spec = OnceTriggerSpec(
        action_kind="message",
        payload="call the dentist",
        fire_at=datetime(2030, 1, 1, 15, 0, tzinfo=UTC),
    )
    assert spec.recurrence == "once"
    ```
    """

    action_kind: TriggerActionKind
    payload: str
    fire_at: datetime
    timezone: str | None = None
    recurrence: Literal["once"] = field(default="once", init=False)


@dataclass(frozen=True, slots=True)
class DailyTriggerSpec:
    """A trigger definition recurring daily at a local wall-clock time."""

    action_kind: TriggerActionKind
    payload: str
    timezone: str
    time_of_day: str
    recurrence: Literal["daily"] = field(default="daily", init=False)


@dataclass(frozen=True, slots=True)
class WeeklyTriggerSpec:
    """A trigger definition recurring weekly at a local wall-clock time."""

    action_kind: TriggerActionKind
    payload: str
    timezone: str
    time_of_day: str
    weekday: int
    recurrence: Literal["weekly"] = field(default="weekly", init=False)


type TriggerSpec = OnceTriggerSpec | DailyTriggerSpec | WeeklyTriggerSpec
"""A strict Scheduled trigger definition selected by recurrence."""


@dataclass(frozen=True, slots=True)
class MaterializedTriggerSchedule:
    """Canonical columns derived from a validated trigger definition."""

    next_fire_at: datetime
    timezone: str
    wall_time: str | None
    weekday: int | None


def _parse_wall_time(value: str) -> time:
    """Parse a `HH:MM` wall-clock time without seconds or an offset."""
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        message = f"wall-clock time must be HH:MM, got {value!r}"
        raise InvalidTriggerSpecError(message) from error
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        message = f"wall-clock time must be HH:MM, got {value!r}"
        raise InvalidTriggerSpecError(message)
    return parsed


def _zone(timezone: str) -> ZoneInfo:
    """Resolve an IANA timezone name into its scheduling rules."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        message = f"unknown timezone: {timezone!r}"
        raise InvalidTriggerSpecError(message) from error


def _materialize_once(
    spec: OnceTriggerSpec, *, now: datetime
) -> MaterializedTriggerSchedule:
    """Normalize one absolute instant while rejecting past or naive values."""
    if spec.fire_at.tzinfo is None:
        message = "a once trigger's fire_at must be timezone-aware"
        raise InvalidTriggerSpecError(message)
    fire_at = spec.fire_at.astimezone(UTC)
    if fire_at < now:
        message = "a once trigger's fire_at must not be in the past"
        raise InvalidTriggerSpecError(message)
    return MaterializedTriggerSchedule(
        next_fire_at=fire_at,
        timezone=spec.timezone or "UTC",
        wall_time=None,
        weekday=None,
    )


def _materialize_recurring(
    spec: DailyTriggerSpec | WeeklyTriggerSpec, *, now: datetime
) -> MaterializedTriggerSchedule:
    """Validate and materialize a recurring wall-clock definition."""
    wall_time = _parse_wall_time(spec.time_of_day)
    if isinstance(spec, WeeklyTriggerSpec) and not 0 <= spec.weekday <= _MAX_WEEKDAY:
        message = f"weekday must be 0..6, got {spec.weekday}"
        raise InvalidTriggerSpecError(message)
    weekday = spec.weekday if isinstance(spec, WeeklyTriggerSpec) else None
    return MaterializedTriggerSchedule(
        next_fire_at=next_recurring_fire(
            spec.recurrence,
            timezone=spec.timezone,
            wall_time=wall_time,
            weekday=weekday,
            after=now,
        ),
        timezone=spec.timezone,
        wall_time=wall_time.isoformat(timespec="minutes"),
        weekday=weekday,
    )


def next_recurring_fire(
    recurrence: Literal["daily", "weekly"],
    *,
    timezone: str,
    wall_time: time,
    weekday: int | None,
    after: datetime,
) -> datetime:
    """Materialize the next local wall-clock occurrence after `after`, as UTC.

    Date arithmetic happens before localization so the requested wall-clock hour
    survives daylight-saving offset changes.
    """
    zone = _zone(timezone)
    local_after = after.astimezone(zone)
    target = datetime.combine(local_after.date(), wall_time)
    if recurrence == "weekly":
        if weekday is None:
            message = "weekly recurrence requires a weekday"
            raise InvalidTriggerSpecError(message)
        target += timedelta(days=(weekday - target.weekday()) % 7)
    candidate = target.replace(tzinfo=zone)
    if candidate <= local_after:
        candidate = (
            target + timedelta(days=7 if recurrence == "weekly" else 1)
        ).replace(tzinfo=zone)
    return candidate.astimezone(UTC)


def materialize_trigger_schedule(
    spec: TriggerSpec, *, now: datetime
) -> MaterializedTriggerSchedule:
    """Validate a strict trigger definition and derive its persisted schedule."""
    if isinstance(spec, OnceTriggerSpec):
        return _materialize_once(spec, now=now)
    return _materialize_recurring(spec, now=now)
