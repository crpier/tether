"""The Recall tethering path: study items, SM-2 spaced prompts, completion gate.

Recall is the second way a loose Memory becomes tethered (ADR 0004): instead of a
human asserting a fact is true (Review), the human *proves they retained* the
material by answering spaced recall prompts correctly across rounds over days.
What tethers is the **distilled learnings** of an educational source (a YouTube
transcript), held as the content of a loose Memory; the raw transcript stays in
the read-only ingested corpus as provenance.

A **study item** pairs that loose Memory with its recall prompts. Each prompt is
an independent **SM-2 card** — the classic SuperMemo-2 spaced-repetition schedule
— whose interval grows as the human answers correctly and collapses back to a
relearning step when they miss. A card is *learned* once it reaches a graduation
threshold of consecutive passing repetitions. When **every** prompt of a study
item is learned the study item is **complete**, and only then is its Memory
tethered; any miss along the way merely reschedules that card, extending the
overall effort.

This module's scheduling math is deliberately pure and model-free: an answer is
reduced to a `(correct, response_ms)` pair, graded to an SM-2 quality, and
applied to a card. Generating prompts from a transcript is the only model-backed
step, and it lives behind an injected collaborator so the schedule stays testable
by driving controlled answers and timestamps.

>>> schedule = initial_schedule(now=datetime(2026, 1, 1, tzinfo=UTC))
>>> reviewed = review_schedule(schedule, quality=5, now=schedule.due_at)
>>> reviewed.repetitions
1
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import ClassVar, Literal, Protocol, runtime_checkable
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel, Json, ValidationError
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Real,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger
from tether.memories import Memory, MemoryProvenance, MemoryService

GRADUATION_REPETITIONS = 3
"""Consecutive passing repetitions after which a prompt counts as learned.

A study item completes (and its Memory tethers) only when every prompt clears
this bar, so it sets how much sustained recall the human must demonstrate.
"""

INITIAL_EASE_FACTOR = 2.5
"""The SM-2 starting ease factor, before any answer adjusts it."""

MIN_EASE_FACTOR = 1.3
"""The SM-2 floor: a card's interval can stop growing but never shrink per-step."""

PASSING_QUALITY = 3
"""The SM-2 quality at or above which a review counts as a pass (recall succeeded)."""

_SECOND_REPETITION = 2
"""The repetition count whose interval is the fixed SM-2 second step."""

_SECOND_INTERVAL_DAYS = 6
"""The fixed SM-2 interval for the second passing repetition, in days."""

_MIN_CHOICES = 2
"""The fewest options a multiple-choice prompt can offer."""

_FAST_RESPONSE_MS = 8_000
"""At or under this response time a correct answer is graded a perfect 5."""

_MEDIUM_RESPONSE_MS = 20_000
"""At or under this response time a correct answer is graded 4; slower is 3."""

_INCORRECT_QUALITY = 1
"""The SM-2 quality assigned to any incorrect answer (a sub-passing blackout)."""


def grade_answer(*, correct: bool, response_ms: int) -> int:
    """Reduce an answer to an SM-2 review quality in the range 0..5.

    Correctness is the dominant signal — a wrong answer always grades below the
    passing threshold regardless of speed — and response time refines a correct
    answer: a fast recall is a confident 5, a slow one a hesitant 3.
    """
    if not correct:
        return _INCORRECT_QUALITY
    if response_ms <= _FAST_RESPONSE_MS:
        return 5
    if response_ms <= _MEDIUM_RESPONSE_MS:
        return 4
    return PASSING_QUALITY


@dataclass(frozen=True, slots=True)
class RecallSchedule:
    """One prompt's SM-2 card state: how far through the schedule it has come.

    `repetitions` counts consecutive passing reviews (it resets to zero on a
    miss); `ease_factor` is the SM-2 multiplier that paces interval growth;
    `interval_days` is the gap to the next review; `due_at` is when that review
    is owed.
    """

    repetitions: int
    ease_factor: float
    interval_days: int
    due_at: datetime


def initial_schedule(*, now: datetime) -> RecallSchedule:
    """The state of a freshly generated prompt: un-learned and due immediately.

    A new card is owed its first review right away so the opening round is
    available the moment a study item is created.
    """
    return RecallSchedule(
        repetitions=0,
        ease_factor=INITIAL_EASE_FACTOR,
        interval_days=0,
        due_at=now,
    )


def _next_ease_factor(ease_factor: float, quality: int) -> float:
    """Apply the SM-2 ease-factor update for a review of `quality`, clamped.

    The standard SM-2 adjustment nudges ease up for confident recall and down
    for hesitant or failed recall, but never below the floor — so a hard card's
    interval can stagnate at the minimum step yet never invert.
    """
    adjusted = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return max(MIN_EASE_FACTOR, adjusted)


def review_schedule(
    schedule: RecallSchedule, *, quality: int, now: datetime
) -> RecallSchedule:
    """Advance an SM-2 card by one review and return its next state.

    A passing quality (>= 3) advances the repetition count and stretches the
    interval — one day, then six, then scaled by the ease factor — while a miss
    resets the card to a one-day relearning step. The ease factor is adjusted on
    every review, pass or miss.
    """
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
        repetitions=repetitions,
        ease_factor=ease_factor,
        interval_days=interval_days,
        due_at=now + timedelta(days=interval_days),
    )


def is_learned(schedule: RecallSchedule) -> bool:
    """Whether a card has reached the graduation threshold of passing repetitions."""
    return schedule.repetitions >= GRADUATION_REPETITIONS


type RecallPromptKind = Literal["multiple_choice"]
"""The form of a recall prompt. Only multiple-choice exists today (issue #20);
short-answer and essay are a deferred extension of this enum."""

type StudyItemState = Literal["studying", "completed"]
"""A study item's lifecycle: drilling its prompts, or fully recalled and tethered."""


class StudyItemNotFoundError(Exception):
    """Raised when an operation targets a study item that does not exist."""


class RecallPromptNotFoundError(Exception):
    """Raised when an operation targets a recall prompt that does not exist."""


class StudyItemExistsError(Exception):
    """Raised when starting Recall for a source already under study.

    A source becomes a study item at most once; re-promoting it would fork the
    schedule and the distilled-learnings Memory, so it is a domain conflict.
    """


class TranscriptNotReadyError(Exception):
    """Raised when starting Recall for a source whose transcript is not fetched.

    Recall distils the transcript, so a source must have one fetched first
    (issue #17's `fetch_transcript`) before it can become a study item.
    """


class InvalidPromptError(Exception):
    """Raised when generated prompts are malformed (no prompts, bad choices)."""


class InvalidAnswerError(Exception):
    """Raised when an answer selects a choice index outside the prompt's range."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


@dataclass(frozen=True, slots=True)
class GeneratedPrompt:
    """A single recall prompt as the generator produces it, before persistence.

    `choices` are the multiple-choice options and `correct_index` points at the
    right one. The index is validated against the choices on the way in, so a
    malformed prompt is a domain error rather than a corrupt row.
    """

    question: str
    choices: list[str]
    correct_index: int


@dataclass(frozen=True, slots=True)
class GeneratedStudyItem:
    """The model's distillation of a source: its learnings plus recall prompts."""

    distilled_learnings: str
    prompts: list[GeneratedPrompt]


@runtime_checkable
class StudyItemGenerator(Protocol):
    """Distils a transcript into learnings + recall prompts (the model-backed step).

    Structural so a controlled fake drives the schedule tests while the live
    implementation runs an ephemeral pi process. This is the *only* part of
    Recall that needs a model — grading and scheduling are pure.
    """

    async def generate(self, *, transcript: str, title: str) -> GeneratedStudyItem:
        """Produce distilled learnings and recall prompts for one source."""
        ...


@runtime_checkable
class AgentTextRunner(Protocol):
    """Runs a prompt through the agent and returns its final text.

    A structural subset of the scheduler's prompt runner, declared here so the
    live generator depends on a capability rather than on the scheduler module.
    """

    async def run(self, prompt: str) -> str:
        """Run `prompt` through the agent and return its final message."""
        ...


_DEFAULT_PROMPT_COUNT = 5
"""How many recall prompts the live generator asks the model to produce."""

_GENERATION_INSTRUCTIONS = """\
You are distilling an educational video transcript into a compact study item for \
spaced-repetition recall.

Return ONLY a JSON object (no prose, no code fences) with this exact shape:
{{
  "distilled_learnings": "<a few sentences capturing the key learnings>",
  "prompts": [
    {{
      "question": "<a multiple-choice question testing one learning>",
      "choices": ["<option 0>", "<option 1>", "<option 2>", "<option 3>"],
      "correct_index": <0-based index of the correct choice>
    }}
  ]
}}

Produce {count} prompts. Each prompt must have at least two choices and exactly \
one correct answer. Base everything strictly on the transcript.

Title: {title}

Transcript:
{transcript}
"""


class _ParsedPrompt(BaseModel):
    """Strict parse target for one model-produced recall prompt."""

    question: str
    choices: list[str]
    correct_index: int


class _ParsedStudyItem(BaseModel):
    """Strict parse target for the model's distillation JSON."""

    distilled_learnings: str
    prompts: list[_ParsedPrompt]


def _extract_json_object(text: str) -> str:
    """Slice the outermost JSON object from a model reply, tolerating stray prose.

    A weaker model may wrap the JSON in code fences or commentary; taking the
    span from the first brace to the last is a forgiving way to recover the
    object without trusting the model to emit it bare.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        message = "model reply contained no JSON object"
        raise InvalidPromptError(message)
    return text[start : end + 1]


class PiStudyItemGenerator:
    """The live `StudyItemGenerator`: distils a transcript via an agent run.

    It prompts the agent for strict JSON, recovers the JSON object from the
    reply, and validates it into a `GeneratedStudyItem`. A reply that is not
    valid JSON or not the expected shape is an `InvalidPromptError`, so a weak
    model degrades to a clean failure rather than a corrupt study item — the
    service then never creates a Memory or prompts.
    """

    def __init__(
        self, runner: AgentTextRunner, *, prompt_count: int = _DEFAULT_PROMPT_COUNT
    ) -> None:
        self.runner: AgentTextRunner = runner
        self.prompt_count: int = prompt_count

    async def generate(self, *, transcript: str, title: str) -> GeneratedStudyItem:
        """Distil a transcript into learnings and multiple-choice recall prompts."""
        prompt = _GENERATION_INSTRUCTIONS.format(
            count=self.prompt_count, title=title, transcript=transcript
        )
        reply = await self.runner.run(prompt)
        try:
            parsed = _ParsedStudyItem.model_validate_json(_extract_json_object(reply))
        except (ValidationError, json.JSONDecodeError) as error:
            message = f"model produced an unusable study item: {error}"
            raise InvalidPromptError(message) from error
        return GeneratedStudyItem(
            distilled_learnings=parsed.distilled_learnings,
            prompts=[
                GeneratedPrompt(
                    question=prompt.question,
                    choices=prompt.choices,
                    correct_index=prompt.correct_index,
                )
                for prompt in parsed.prompts
            ],
        )


class StudyItem[S = Pending](Model[S, "StudyItem[Fetched]"]):
    """A loose Memory under Recall, paired with the source it was distilled from."""

    id: StudyItem.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    memory_id: StudyItem.Col[UUID7] = Text()
    """The loose Memory holding the distilled learnings; tethered on completion."""
    source_video_id: StudyItem.Col[str] = Text(unique=True)
    """The ingested YouTube video the learnings were distilled from."""
    source_title: StudyItem.Col[str] = Text()
    """The source's human-facing title, for the recall surface."""
    state: StudyItem.Col[StudyItemState] = Text()
    """`studying` while prompts are being drilled; `completed` once all learned."""
    created_at: StudyItem.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: StudyItem.GenCol[datetime] = Text(default=CurrentTimestamp)
    completed_at: StudyItem.Col[datetime | None] = Text(default=None, nullable=True)

    __indexes__: ClassVar = [Index(state)]


class RecallPrompt[S = Pending](Model[S, "RecallPrompt[Fetched]"]):
    """One recall prompt: its question, choices, answer, and SM-2 card state."""

    id: RecallPrompt.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    study_item_id: RecallPrompt.Col[UUID7] = Text()
    """The study item this prompt drills."""
    kind: RecallPrompt.Col[RecallPromptKind] = Text()
    question: RecallPrompt.Col[str] = Text()
    choices: RecallPrompt.Col[Json[list[str]]] = Text()
    """The multiple-choice options, as JSON."""
    correct_index: RecallPrompt.Col[int] = Integer()
    """Index into `choices` of the right answer; never sent to the client."""
    repetitions: RecallPrompt.Col[int] = Integer(default=0)
    """Consecutive passing reviews; resets to zero on a miss (SM-2)."""
    ease_factor: RecallPrompt.Col[float] = Real(default=INITIAL_EASE_FACTOR)
    """The SM-2 interval multiplier; clamped at `MIN_EASE_FACTOR`."""
    interval_days: RecallPrompt.Col[int] = Integer(default=0)
    """Days until the next review of this card."""
    due_at: RecallPrompt.Col[datetime] = Text()
    """When this prompt's next review is owed, as UTC."""
    created_at: RecallPrompt.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: RecallPrompt.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(study_item_id, due_at)]


class RecallAnswer[S = Pending](Model[S, "RecallAnswer[Fetched]"]):
    """An append-only record of one answered prompt: the input and its grading."""

    id: RecallAnswer.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    prompt_id: RecallAnswer.Col[UUID7] = Text()
    selected_index: RecallAnswer.Col[int] = Integer()
    correct: RecallAnswer.Col[bool] = Integer()
    response_ms: RecallAnswer.Col[int] = Integer()
    quality: RecallAnswer.Col[int] = Integer()
    """The SM-2 quality the answer graded to (0..5)."""
    answered_at: RecallAnswer.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(prompt_id)]


def schedule_of(prompt: RecallPrompt[Fetched]) -> RecallSchedule:
    """Read a stored prompt's SM-2 card state as a `RecallSchedule`."""
    return RecallSchedule(
        repetitions=prompt.repetitions,
        ease_factor=prompt.ease_factor,
        interval_days=prompt.interval_days,
        due_at=prompt.due_at,
    )


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """The result of answering a prompt: its new card state and any completion.

    `completed` is true on the answer that learns the study item's last prompt;
    `tethered` is true when that completion tethered the distilled-learnings
    Memory (it is false only if a prior human Review had already tethered it).
    """

    prompt: RecallPrompt[Fetched]
    correct: bool
    quality: int
    completed: bool
    tethered: bool


@dataclass(frozen=True, slots=True)
class DuePrompt:
    """A prompt currently owed a review, with the study item it belongs to."""

    prompt: RecallPrompt[Fetched]
    study_item: StudyItem[Fetched]


def _validate_generated(generated: GeneratedStudyItem) -> None:
    """Reject a distillation with no prompts or an out-of-range correct index."""
    if not generated.prompts:
        message = "a study item requires at least one recall prompt"
        raise InvalidPromptError(message)
    for prompt in generated.prompts:
        if len(prompt.choices) < _MIN_CHOICES:
            message = "a multiple-choice prompt requires at least two choices"
            raise InvalidPromptError(message)
        if not 0 <= prompt.correct_index < len(prompt.choices):
            message = (
                f"correct_index {prompt.correct_index} is outside the "
                f"{len(prompt.choices)} choices"
            )
            raise InvalidPromptError(message)


class RecallService:
    """Capability surface for the Recall tethering path, over a snekql database.

    Starting Recall distils a transcript (the one model-backed step, injected)
    into a loose Memory plus its prompts; answering grades and reschedules a
    prompt with pure SM-2 math; learning the last prompt completes the study item
    and tethers its Memory through the shared `MemoryService` — so the second
    gate reuses the one Memory lifecycle rather than a parallel one.
    """

    def __init__(
        self,
        database: Database,
        memory_service: MemoryService,
        generator: StudyItemGenerator,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.memory_service: MemoryService = memory_service
        self.generator: StudyItemGenerator = generator
        self.tracer: Tracer = tracer
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def start_recall(
        self,
        *,
        source_video_id: str,
        source_title: str,
        transcript: str,
        now: datetime,
        logger: Logger,
    ) -> StudyItem[Fetched]:
        """Turn an educational source into a study item drilling its prompts.

        The transcript is distilled (model) into learnings — captured as a loose
        Memory with YouTube provenance — and recall prompts, each an SM-2 card
        due immediately so the first round is available at once. A source already
        under study conflicts rather than forking a second schedule.
        """
        with self.tracer.start_as_current_span(
            "RecallService.start_recall",
            attributes={"recall.source_video_id": source_video_id},
        ):
            _debug(logger, "Starting Recall", source_video_id=source_video_id)
            await self._require_absent(source_video_id)
            generated = await self.generator.generate(
                transcript=transcript, title=source_title
            )
            _validate_generated(generated)
            memory = await self.memory_service.capture(
                generated.distilled_learnings,
                provenance=MemoryProvenance(kind="youtube"),
                logger=logger,
            )

            async def _start_recall(tx: Transaction) -> StudyItem[Fetched]:
                study_item = await tx.execute(
                    insert(
                        StudyItem(
                            memory_id=memory.id,
                            source_video_id=source_video_id,
                            source_title=source_title,
                            state="studying",
                        )
                    ).returning()
                )
                for generated_prompt in generated.prompts:
                    schedule = initial_schedule(now=now)
                    _ = await tx.execute(
                        insert(
                            RecallPrompt(
                                study_item_id=study_item.id,
                                kind="multiple_choice",
                                question=generated_prompt.question,
                                choices=generated_prompt.choices,
                                correct_index=generated_prompt.correct_index,
                                repetitions=schedule.repetitions,
                                ease_factor=schedule.ease_factor,
                                interval_days=schedule.interval_days,
                                due_at=schedule.due_at,
                            )
                        )
                    )
                return study_item

            study_item = await run_in_transaction(self.database, _start_recall)
        _info(
            logger,
            "Recall started",
            study_item_id=str(study_item.id),
            memory_id=str(memory.id),
            prompt_count=len(generated.prompts),
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["recall"]))
        return study_item

    async def list_study_items(self, *, logger: Logger) -> list[StudyItem[Fetched]]:
        """List every study item, newest-first, for the recall surface."""
        _debug(logger, "Listing study items")
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(StudyItem).all().order_by(StudyItem.created_at.desc())
            )

    async def list_due_prompts(
        self, now: datetime, *, limit: int | None = None, logger: Logger
    ) -> list[DuePrompt]:
        """List prompts owed a review now, across still-studying items.

        This is the pull-based recall surface: the outstanding prompts are those
        whose `due_at` has arrived on a study item that has not yet completed,
        soonest-due first. `limit` caps the rows returned (`None` is unbounded);
        eligibility is filtered after the query, so the cap is applied last to
        keep the soonest-due prompts.
        """
        _debug(logger, "Listing due recall prompts")
        async with self.database.transaction() as tx:
            studying = await tx.fetch_all(
                select(StudyItem).where(StudyItem.state.eq("studying"))
            )
            items_by_id = {item.id: item for item in studying}
            if not items_by_id:
                return []
            prompts = await tx.fetch_all(
                select(RecallPrompt)
                .where(RecallPrompt.due_at.lte(now))
                .order_by(RecallPrompt.due_at.asc())
            )
        due = [
            DuePrompt(prompt=prompt, study_item=items_by_id[prompt.study_item_id])
            for prompt in prompts
            if prompt.study_item_id in items_by_id
        ]
        if limit is not None:
            due = due[:limit]
        _debug(logger, "Due recall prompts listed", result_count=len(due))
        return due

    async def fetch_prompt(self, prompt_id: UUID7) -> RecallPrompt[Fetched]:
        """Fetch a recall prompt by id, or raise when absent."""
        async with self.database.transaction() as tx:
            return await self._fetch_prompt(tx, prompt_id)

    async def answer_prompt(
        self,
        prompt: RecallPrompt[Fetched],
        *,
        selected_index: int,
        response_ms: int,
        now: datetime,
        logger: Logger,
    ) -> AnswerOutcome:
        """Grade and reschedule a prompt, completing the study item when learned.

        The selected choice is graded deterministically, reduced to an SM-2
        quality with the response time, and applied to the card. The answer is
        recorded for audit. When this learns the study item's final prompt the
        item completes and its Memory tethers (the Recall gate); a miss simply
        reschedules, extending the overall effort. Tethering happens **only** on
        full completion.
        """
        if not 0 <= selected_index < len(prompt.choices):
            message = (
                f"selected_index {selected_index} is outside the "
                f"{len(prompt.choices)} choices"
            )
            raise InvalidAnswerError(message)
        correct = selected_index == prompt.correct_index
        quality = grade_answer(correct=correct, response_ms=response_ms)
        reviewed = review_schedule(schedule_of(prompt), quality=quality, now=now)
        with self.tracer.start_as_current_span(
            "RecallService.answer_prompt",
            attributes={
                "recall.prompt_id": str(prompt.id),
                "recall.correct": correct,
                "recall.quality": quality,
            },
        ):

            async def _answer(
                tx: Transaction,
            ) -> tuple[RecallPrompt[Fetched], StudyItem[Fetched], bool]:
                _ = await tx.execute(
                    update(RecallPrompt)
                    .set(RecallPrompt.repetitions.to(reviewed.repetitions))
                    .set(RecallPrompt.ease_factor.to(reviewed.ease_factor))
                    .set(RecallPrompt.interval_days.to(reviewed.interval_days))
                    .set(RecallPrompt.due_at.to(reviewed.due_at))
                    .set(RecallPrompt.updated_at.to(CurrentTimestamp))
                    .where(RecallPrompt.id.eq(prompt.id))
                )
                _ = await tx.execute(
                    insert(
                        RecallAnswer(
                            prompt_id=prompt.id,
                            selected_index=selected_index,
                            correct=correct,
                            response_ms=response_ms,
                            quality=quality,
                        )
                    )
                )
                fresh_prompt = await self._fetch_prompt(tx, prompt.id)
                study_item = await self._fetch_study_item(tx, prompt.study_item_id)
                siblings = await tx.fetch_all(
                    select(RecallPrompt).where(
                        RecallPrompt.study_item_id.eq(study_item.id)
                    )
                )
                newly_complete = study_item.state == "studying" and all(
                    is_learned(schedule_of(sibling)) for sibling in siblings
                )
                if newly_complete:
                    _ = await tx.execute(
                        update(StudyItem)
                        .set(StudyItem.state.to("completed"))
                        .set(StudyItem.completed_at.to(now))
                        .set(StudyItem.updated_at.to(CurrentTimestamp))
                        .where(StudyItem.id.eq(study_item.id))
                    )
                return fresh_prompt, study_item, newly_complete

            fresh_prompt, study_item, newly_complete = await run_in_transaction(
                self.database, _answer
            )
        tethered = False
        if newly_complete:
            tethered = await self._tether_memory(study_item.memory_id, logger=logger)
            _info(
                logger,
                "Recall completed",
                study_item_id=str(study_item.id),
                memory_id=str(study_item.memory_id),
                tethered=tethered,
            )
        _info(
            logger,
            "Recall prompt answered",
            prompt_id=str(prompt.id),
            correct=correct,
            quality=quality,
            completed=newly_complete,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["recall"]))
        return AnswerOutcome(
            prompt=fresh_prompt,
            correct=correct,
            quality=quality,
            completed=newly_complete,
            tethered=tethered,
        )

    async def _tether_memory(self, memory_id: UUID7, *, logger: Logger) -> bool:
        """Tether the distilled-learnings Memory on full Recall completion.

        Returns whether this call performed the tether. A human Review may have
        already tethered the same loose Memory; that is convergent, not an error,
        so an already-tethered Memory leaves completion intact and reports False.
        """
        async with self.database.transaction() as tx:
            memory = await tx.fetch_one_or_none(
                select(Memory).where(Memory.id.eq(memory_id))
            )
        if memory is None:
            logger.warning(
                "Recall completion found no Memory to tether",
                memory_id=str(memory_id),
            )
            return False
        if memory.tethered_at is not None:
            return False
        _ = await self.memory_service.tether(memory, logger=logger)
        return True

    async def _require_absent(self, source_video_id: str) -> None:
        """Raise if a study item already exists for the source video."""
        async with self.database.transaction() as tx:
            existing = await tx.fetch_one_or_none(
                select(StudyItem).where(StudyItem.source_video_id.eq(source_video_id))
            )
        if existing is not None:
            message = f"video {source_video_id} is already under Recall"
            raise StudyItemExistsError(message)

    async def _fetch_prompt(
        self, tx: Transaction, prompt_id: UUID7
    ) -> RecallPrompt[Fetched]:
        """Fetch a recall prompt by id or raise."""
        prompt = await tx.fetch_one_or_none(
            select(RecallPrompt).where(RecallPrompt.id.eq(prompt_id))
        )
        if prompt is None:
            raise RecallPromptNotFoundError(prompt_id)
        return prompt

    async def _fetch_study_item(
        self, tx: Transaction, study_item_id: UUID7
    ) -> StudyItem[Fetched]:
        """Fetch a study item by id or raise."""
        item = await tx.fetch_one_or_none(
            select(StudyItem).where(StudyItem.id.eq(study_item_id))
        )
        if item is None:
            raise StudyItemNotFoundError(study_item_id)
        return item


async def create_recall_schema(database: Database) -> None:
    """Create the study-item, recall-prompt, and answer tables on an initialized DB.

    Applied as its own ordered migrations after the earlier schemas (prefix
    `007_`). Scaffolding emits one statement per table/index, and a snekql
    migration body runs exactly one statement, so each becomes its own ordered
    migration.
    """
    migrations = {
        f"007_{label}": sql
        for label, sql in scaffold_sqlite_statements(
            [StudyItem, RecallPrompt, RecallAnswer]
        )
    }
    await database.migrate(migrations)
