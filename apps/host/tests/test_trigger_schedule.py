"""Behavior tests for Scheduled trigger schedule materialization."""

from datetime import UTC, datetime, time

from snektest import assert_eq, assert_raises, test

from tether.trigger_capabilities import TriggerSpecBody
from tether.trigger_schedule import (
    DailyTriggerSpec,
    InvalidTriggerSpecError,
    OnceTriggerSpec,
    WeeklyTriggerSpec,
    materialize_trigger_schedule,
    next_recurring_fire,
)


@test()
def a_daily_schedule_materializes_its_next_local_occurrence() -> None:
    """A daily schedule keeps its wall time while producing a UTC fire instant."""
    materialized = materialize_trigger_schedule(
        DailyTriggerSpec(
            action_kind="message",
            payload="stand up",
            timezone="America/New_York",
            time_of_day="09:00",
        ),
        now=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
    )

    assert_eq(materialized.next_fire_at, datetime(2026, 7, 2, 13, 0, tzinfo=UTC))
    assert_eq(materialized.wall_time, "09:00")


@test()
def a_daily_schedule_preserves_local_time_across_spring_dst() -> None:
    """A local recurrence derives the new UTC offset after a DST transition."""
    next_fire_at = next_recurring_fire(
        "daily",
        timezone="America/New_York",
        wall_time=time(9),
        weekday=None,
        after=datetime(2026, 3, 7, 14, 0, tzinfo=UTC),
    )

    assert_eq(next_fire_at, datetime(2026, 3, 8, 13, 0, tzinfo=UTC))


@test()
def a_weekly_schedule_materializes_the_next_matching_weekday() -> None:
    """A weekly definition selects its next local weekday occurrence."""
    materialized = materialize_trigger_schedule(
        WeeklyTriggerSpec(
            action_kind="message",
            payload="weekly review",
            timezone="UTC",
            time_of_day="08:30",
            weekday=4,
        ),
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )

    assert_eq(materialized.next_fire_at, datetime(2030, 1, 4, 8, 30, tzinfo=UTC))


@test()
def a_once_schedule_normalizes_its_absolute_instant_to_utc() -> None:
    """A once definition keeps its instant while normalizing the offset."""
    materialized = materialize_trigger_schedule(
        OnceTriggerSpec(
            action_kind="message",
            payload="call the dentist",
            fire_at=datetime.fromisoformat("2030-01-01T10:00:00-05:00"),
        ),
        now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
    )

    assert_eq(materialized.next_fire_at, datetime(2030, 1, 1, 15, 0, tzinfo=UTC))


@test()
def a_daily_request_becomes_a_strict_daily_definition() -> None:
    """The request boundary removes fields that are meaningless for a daily rule."""
    spec = TriggerSpecBody(
        recurrence="daily",
        action_kind="prompt",
        payload="summarize my day",
        timezone="UTC",
        time_of_day="18:00",
    ).to_spec()

    assert isinstance(spec, DailyTriggerSpec)


@test()
def a_weekly_request_requires_a_weekday() -> None:
    """The request boundary rejects an incomplete weekly definition."""
    with assert_raises(InvalidTriggerSpecError):
        _ = TriggerSpecBody(
            recurrence="weekly",
            action_kind="message",
            payload="weekly review",
            timezone="UTC",
            time_of_day="09:00",
        ).to_spec()


@test()
def a_daily_request_rejects_an_absolute_instant() -> None:
    """The request boundary rejects once-only fields on a daily definition."""
    with assert_raises(InvalidTriggerSpecError):
        _ = TriggerSpecBody(
            recurrence="daily",
            action_kind="message",
            payload="stand up",
            timezone="UTC",
            time_of_day="09:00",
            fire_at=datetime(2030, 1, 1, tzinfo=UTC),
        ).to_spec()
