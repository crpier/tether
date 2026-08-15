"""Pure scheduling policy for spaced Recall prompts.

An answer is reduced to a quality score and applied to an independent SM-2 card.
The policy has no persistence or model dependencies, so callers can advance
controlled schedules deterministically.

>>> from datetime import UTC, datetime
>>> schedule = initial_schedule(now=datetime(2026, 1, 1, tzinfo=UTC))
>>> review_schedule(schedule, quality=5, now=schedule.due_at).repetitions
1
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

GRADUATION_REPETITIONS = 3
"""Consecutive passing repetitions after which a prompt counts as learned."""

INITIAL_EASE_FACTOR = 2.5
"""The SM-2 starting ease factor, before any answer adjusts it."""

MIN_EASE_FACTOR = 1.3
"""The SM-2 floor below which a card's ease factor cannot fall."""

PASSING_QUALITY = 3
"""The minimum SM-2 quality at which a review advances the card."""

_SECOND_REPETITION = 2
_SECOND_INTERVAL_DAYS = 6
_FAST_RESPONSE_MS = 8_000
_MEDIUM_RESPONSE_MS = 20_000
_INCORRECT_QUALITY = 1
_FREE_TEXT_CORRECT_QUALITY = 4


type RecallPromptKind = Literal["multiple_choice", "short_answer", "essay"]
"""The form of a recall prompt and the grading policy it selects."""


@dataclass(frozen=True, slots=True)
class RecallSchedule:
    """One prompt's SM-2 card state."""

    due_at: datetime
    ease_factor: float
    interval_days: int
    repetitions: int


def initial_schedule(*, now: datetime) -> RecallSchedule:
    """Create an unlearned card due immediately."""
    return RecallSchedule(
        due_at=now,
        ease_factor=INITIAL_EASE_FACTOR,
        interval_days=0,
        repetitions=0,
    )


def _next_ease_factor(ease_factor: float, quality: int) -> float:
    """Apply the SM-2 adjustment while preserving the minimum ease factor."""
    adjusted = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return max(MIN_EASE_FACTOR, adjusted)


def review_schedule(
    schedule: RecallSchedule, *, quality: int, now: datetime
) -> RecallSchedule:
    """Advance an SM-2 card by one review and return its next state."""
    ease_factor = _next_ease_factor(schedule.ease_factor, quality)
    if quality < PASSING_QUALITY:
        repetitions = 0
        interval_days = 1
    else:
        repetitions = schedule.repetitions + 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == _SECOND_REPETITION:
            interval_days = _SECOND_INTERVAL_DAYS
        else:
            interval_days = round(schedule.interval_days * ease_factor)
    return replace(
        schedule,
        due_at=now + timedelta(days=interval_days),
        ease_factor=ease_factor,
        interval_days=interval_days,
        repetitions=repetitions,
    )


def grade_answer(*, correct: bool, response_ms: int, kind: RecallPromptKind) -> int:
    """Reduce an answer to an SM-2 quality in the range 0 through 5."""
    if not correct:
        return _INCORRECT_QUALITY
    if kind != "multiple_choice":
        return _FREE_TEXT_CORRECT_QUALITY
    if response_ms <= _FAST_RESPONSE_MS:
        return 5
    if response_ms <= _MEDIUM_RESPONSE_MS:
        return 4
    return PASSING_QUALITY


def is_learned(schedule: RecallSchedule) -> bool:
    """Return whether a card has reached the graduation threshold."""
    return schedule.repetitions >= GRADUATION_REPETITIONS
