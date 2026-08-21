"""Stable Health Connect enum and time normalization for telemetry reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_EXERCISE_TYPE_LABELS = {
    56: "running",
    79: "walking",
}
"""Health Connect exercise labels needed by agent summaries."""

_SLEEP_STAGE_LABELS = {
    1: "awake",
    2: "sleeping",
    3: "out_of_bed",
    4: "light",
    5: "deep",
    6: "rem",
    7: "awake_in_bed",
}
"""Health Connect sleep-stage labels needed by agent summaries."""


def render_exercise_type(exercise_type: int | None) -> str | None:
    """Render Health Connect exercise enum values for agent-facing reads."""
    if exercise_type is None:
        return None
    return _EXERCISE_TYPE_LABELS.get(exercise_type, f"unknown_{exercise_type}")


def render_sleep_stage(stage: int) -> str:
    """Render Health Connect sleep-stage enum values for agent-facing reads."""
    return _SLEEP_STAGE_LABELS.get(stage, f"unknown_{stage}")


def local_record_date(start_time: int | None, zone_offset_seconds: int | None) -> str:
    """Bucket records by their captured local date when Health Connect provides it."""
    if start_time is None:
        return "unknown"
    return (
        (
            datetime.fromtimestamp(start_time / 1000, UTC)
            + timedelta(seconds=zone_offset_seconds or 0)
        )
        .date()
        .isoformat()
    )


def datetime_from_millis(value: int | None) -> datetime | None:
    """Render Health Connect epoch milliseconds as an unambiguous UTC instant."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000, UTC)


def millis_from_datetime(value: datetime) -> int:
    """Convert a validated aware tool-boundary instant to epoch milliseconds."""
    return int(value.timestamp() * 1_000)


def duration_minutes(start_time: int | None, end_time: int | None) -> float:
    """Return a non-negative interval duration in minutes."""
    if start_time is None or end_time is None:
        return 0.0
    return max(0.0, (end_time - start_time) / 60_000)


def latest_bound(latest_end: int | None, latest_start: int | None) -> int | None:
    """Use the latest available endpoint while retaining instant-only records."""
    bounds = [value for value in (latest_end, latest_start) if value is not None]
    return max(bounds) if bounds else None
