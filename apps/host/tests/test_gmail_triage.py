"""Public policy tests for Gmail triage outcomes."""

from __future__ import annotations

from datetime import UTC, datetime

from snektest import assert_eq, test

from tether.gmail.triage import gmail_deadline_fire_at


@test()
async def deadline_reminders_fire_the_morning_before_in_local_time() -> None:
    """A future deadline maps to 09:00 on the previous local calendar day."""
    fire_at = gmail_deadline_fire_at(
        datetime(2026, 1, 3, 18, tzinfo=UTC),
        now=datetime(2026, 1, 1, 12, tzinfo=UTC),
        timezone_name="UTC",
    )

    assert_eq(fire_at, datetime(2026, 1, 2, 9, tzinfo=UTC))
