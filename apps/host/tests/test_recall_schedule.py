"""Behavior tests for the pure SM-2 recall scheduling core.

These drive the deterministic scheduling math directly — no database, no model,
no HTTP. They prove the load-bearing Recall behavior: how an answer's
correctness and response time map to a review quality, how that quality advances
or resets an SM-2 card, and when a card is considered learned. Driving
controlled answers + timestamps keeps the scheduling logic testable without ever
touching a live model (issue #20).
"""

from datetime import UTC, datetime, timedelta

from snektest import Param, assert_eq, assert_in, test

from tether.recall import (
    GRADUATION_REPETITIONS,
    INITIAL_EASE_FACTOR,
    MIN_EASE_FACTOR,
    RecallPromptKind,
    RecallSchedule,
    grade_answer,
    initial_schedule,
    is_learned,
    review_schedule,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@test(
    [
        Param(value=(1_000, 5), name="correct_fast"),
        Param(value=(12_000, 4), name="correct_medium"),
        Param(value=(30_000, 3), name="correct_slow"),
    ]
)
def correct_multiple_choice_grades_by_response_time(value: tuple[int, int]) -> None:
    """A correct multiple-choice answer's quality drops as the response slows."""
    response_ms, expected = value
    assert_eq(
        grade_answer(correct=True, response_ms=response_ms, kind="multiple_choice"),
        expected,
    )


FREE_TEXT_PARAMS: list[Param[RecallPromptKind]] = [
    Param(value="short_answer", name="short_answer"),
    Param(value="essay", name="essay"),
]

ALL_KIND_PARAMS: list[Param[RecallPromptKind]] = [
    Param(value="multiple_choice", name="multiple_choice"),
    *FREE_TEXT_PARAMS,
]


@test(FREE_TEXT_PARAMS)
def correct_free_text_grades_a_fixed_quality_regardless_of_time(
    kind: RecallPromptKind,
) -> None:
    """Typing/composing time carries no recall signal for free-text kinds."""
    fast = grade_answer(correct=True, response_ms=1_000, kind=kind)
    slow = grade_answer(correct=True, response_ms=300_000, kind=kind)
    assert_eq(fast, 4)
    assert_eq(slow, 4)


@test(ALL_KIND_PARAMS)
def incorrect_answer_grades_below_passing(kind: RecallPromptKind) -> None:
    """Any incorrect answer grades below the SM-2 passing threshold of 3."""
    quality = grade_answer(correct=False, response_ms=500, kind=kind)
    assert_in(quality, (0, 1, 2))


@test()
def initial_schedule_is_due_immediately() -> None:
    """A fresh card starts un-learned, at the default ease, due right away."""
    schedule = initial_schedule(now=NOW)
    assert_eq(schedule.repetitions, 0)
    assert_eq(schedule.ease_factor, INITIAL_EASE_FACTOR)
    assert_eq(schedule.interval_days, 0)
    assert_eq(schedule.due_at, NOW)
    assert_eq(is_learned(schedule), False)


@test()
def first_correct_review_schedules_one_day_out() -> None:
    """The first passing review advances to one repetition, one day out."""
    schedule = initial_schedule(now=NOW)

    reviewed = review_schedule(schedule, quality=5, now=NOW)

    assert_eq(reviewed.repetitions, 1)
    assert_eq(reviewed.interval_days, 1)
    assert_eq(reviewed.due_at, NOW + timedelta(days=1))


@test()
def second_correct_review_schedules_six_days_out() -> None:
    """The second passing review uses the fixed six-day SM-2 step."""
    schedule = review_schedule(initial_schedule(now=NOW), quality=4, now=NOW)

    reviewed = review_schedule(schedule, quality=4, now=NOW)

    assert_eq(reviewed.repetitions, 2)
    assert_eq(reviewed.interval_days, 6)


@test()
def third_correct_review_scales_interval_by_ease() -> None:
    """From the third repetition the interval grows by the ease factor."""
    schedule = RecallSchedule(
        repetitions=2,
        ease_factor=2.5,
        interval_days=6,
        due_at=NOW,
    )

    reviewed = review_schedule(schedule, quality=5, now=NOW)

    assert_eq(reviewed.repetitions, 3)
    assert_eq(reviewed.interval_days, round(6 * reviewed.ease_factor))


@test()
def failing_review_resets_repetitions_and_interval() -> None:
    """A failed review drops the card back to a one-day relearning step."""
    schedule = RecallSchedule(
        repetitions=4,
        ease_factor=2.6,
        interval_days=40,
        due_at=NOW,
    )

    reviewed = review_schedule(schedule, quality=1, now=NOW)

    assert_eq(reviewed.repetitions, 0)
    assert_eq(reviewed.interval_days, 1)
    assert_eq(reviewed.due_at, NOW + timedelta(days=1))
    assert_eq(is_learned(reviewed), False)


@test()
def ease_factor_never_drops_below_the_floor() -> None:
    """Repeated low-quality reviews clamp the ease factor at its floor."""
    schedule = RecallSchedule(
        repetitions=0,
        ease_factor=MIN_EASE_FACTOR,
        interval_days=0,
        due_at=NOW,
    )

    reviewed = review_schedule(schedule, quality=3, now=NOW)

    assert_eq(reviewed.ease_factor, MIN_EASE_FACTOR)


@test()
def a_card_is_learned_once_it_reaches_the_graduation_threshold() -> None:
    """A card graduates after enough consecutive passing repetitions."""
    schedule = initial_schedule(now=NOW)
    for _ in range(GRADUATION_REPETITIONS):
        schedule = review_schedule(schedule, quality=5, now=schedule.due_at)

    assert_eq(schedule.repetitions, GRADUATION_REPETITIONS)
    assert_eq(is_learned(schedule), True)
