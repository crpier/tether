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
from dataclasses import dataclass, field, replace
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

_FREE_TEXT_CORRECT_QUALITY = 4
"""The fixed SM-2 quality for a correct short-answer or essay.

The response-time thresholds above are tuned for a multiple-choice click;
typing a phrase or composing an essay takes far longer without implying
hesitant recall, so time carries no signal for free-text kinds. A flat 4
("correct after some thought") passes without the ease inflation a perfect 5
would grant on every essay.
"""


def grade_answer(*, correct: bool, response_ms: int, kind: RecallPromptKind) -> int:
    """Reduce an answer to an SM-2 review quality in the range 0..5.

    Correctness is the dominant signal — a wrong answer always grades below the
    passing threshold regardless of speed. Response time refines a correct
    answer only for multiple choice (a fast click is a confident 5, a slow one
    a hesitant 3); free-text kinds grade the fixed `_FREE_TEXT_CORRECT_QUALITY`
    because writing time is composition, not recall hesitation.
    """
    if not correct:
        return _INCORRECT_QUALITY
    if kind != "multiple_choice":
        return _FREE_TEXT_CORRECT_QUALITY
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


type RecallPromptKind = Literal["multiple_choice", "short_answer", "essay"]
"""The form of a recall prompt (issue #131).

Multiple-choice grades deterministically against `correct_index`; short-answer
grades free text against `reference_answer` (model-assisted with a strict-match
fallback); essay grades free text against `rubric`, with the model proposing a
grade the **human** confirms (ADR 0004: the model never self-certifies learning).
"""

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

    Each kind carries its grading payload: multiple-choice its `choices` plus
    the `correct_index` pointing at the right one, short-answer its
    `reference_answer`, essay its `rubric`. The payload is validated against the
    kind on the way in, so a malformed prompt is a domain error rather than a
    corrupt row.
    """

    question: str
    kind: RecallPromptKind = "multiple_choice"
    choices: list[str] = field(default_factory=list[str])
    correct_index: int | None = None
    reference_answer: str | None = None
    rubric: str | None = None


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
      "kind": "multiple_choice",
      "question": "<a multiple-choice question testing one learning>",
      "choices": ["<option 0>", "<option 1>", "<option 2>", "<option 3>"],
      "correct_index": <0-based index of the correct choice>
    }},
    {{
      "kind": "short_answer",
      "question": "<a question answered in a word or short phrase>",
      "reference_answer": "<the expected answer, used to grade free text>"
    }},
    {{
      "kind": "essay",
      "question": "<a prompt asking the learner to explain a learning in depth>",
      "rubric": "<what a strong answer must cover, used to grade the essay>"
    }}
  ]
}}

Produce {count} prompts, mixing the kinds: mostly multiple_choice and \
short_answer, and at most one essay. Each multiple_choice prompt must have at \
least two choices and exactly one correct answer. Base everything strictly on \
the transcript.

Title: {title}

Transcript:
{transcript}
"""


class _ParsedPrompt(BaseModel):
    """Strict parse target for one model-produced recall prompt.

    `kind` defaults to multiple-choice so a legacy reply without the field
    still parses; the per-kind payload is validated later, by
    `_validate_generated`, as a domain rule rather than a parse rule.
    """

    kind: RecallPromptKind = "multiple_choice"
    question: str
    choices: list[str] = []
    correct_index: int | None = None
    reference_answer: str | None = None
    rubric: str | None = None


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
        """Distil a transcript into learnings and a mix of recall prompts."""
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
                    kind=prompt.kind,
                    choices=prompt.choices,
                    correct_index=prompt.correct_index,
                    reference_answer=prompt.reference_answer,
                    rubric=prompt.rubric,
                )
                for prompt in parsed.prompts
            ],
        )


class AnswerGradingUnavailableError(Exception):
    """Raised when the model-backed grader cannot produce a verdict.

    Distinct from a wrong answer: the caller falls back (strict match for short
    answers, an empty proposal for essays) instead of failing the card.
    """


@dataclass(frozen=True, slots=True)
class EssayGradeProposal:
    """The model's *proposed* grade for an essay, awaiting human confirmation.

    `correct` is `None` when no proposal could be made (model unavailable); the
    human then grades unaided. The proposal is never applied to SM-2 directly —
    only the human-confirmed grade is (ADR 0004).
    """

    correct: bool | None
    reasoning: str | None


@runtime_checkable
class AnswerGrader(Protocol):
    """Grades free-text answers (the model-backed step of answering).

    Structural so a controlled fake drives the service tests while the live
    implementation runs an ephemeral pi process, the same seam as generation.
    """

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> bool:
        """Judge whether a free-text answer matches the reference answer."""
        ...

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> EssayGradeProposal:
        """Propose a grade for an essay against its rubric, for the human."""
        ...


_SHORT_ANSWER_GRADING_INSTRUCTIONS = """\
You are grading one short-answer recall response.

Question: {question}
Reference answer: {reference_answer}
Learner's answer: {answer_text}

The learner's answer is correct when it conveys the same fact as the reference
answer, allowing different wording. Return ONLY a JSON object (no prose, no
code fences) with this exact shape:
{{"correct": <true or false>}}
"""

_ESSAY_GRADING_INSTRUCTIONS = """\
You are proposing a grade for one essay recall response. A human will review
your proposal and make the final call, so explain your reasoning briefly.

Essay prompt: {question}
Rubric: {rubric}
Learner's essay: {answer_text}

The essay passes when it covers what the rubric requires. Return ONLY a JSON
object (no prose, no code fences) with this exact shape:
{{"correct": <true or false>, "reasoning": "<one or two sentences>"}}
"""


class _ParsedShortAnswerGrade(BaseModel):
    """Strict parse target for the model's short-answer verdict JSON."""

    correct: bool


class _ParsedEssayGrade(BaseModel):
    """Strict parse target for the model's essay grade-proposal JSON."""

    correct: bool
    reasoning: str = ""


_UNUSABLE_GRADING_REPLY_ERRORS = (
    InvalidPromptError,
    ValidationError,
    json.JSONDecodeError,
)
"""The reply-parsing failures the grader converts to *unavailable*.

The same narrow set `PiStudyItemGenerator.generate` recovers from, plus the
`InvalidPromptError` `_extract_json_object` raises for a reply with no JSON
object. Anything else — the run itself failing, a bug in this path —
propagates rather than silently downgrading every answer to the fallback
grade.
"""


class PiAnswerGrader:
    """The live `AnswerGrader`: judges free-text answers via an agent run.

    An unparseable reply degrades to `AnswerGradingUnavailableError` so the
    caller can fall back instead of trusting a garbled verdict. Only reply
    parsing is caught (see `_UNUSABLE_GRADING_REPLY_ERRORS`); a failing run is
    a real error and surfaces as one.
    """

    def __init__(self, runner: AgentTextRunner) -> None:
        self.runner: AgentTextRunner = runner

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> bool:
        """Judge a free-text answer against the reference via the model."""
        prompt = _SHORT_ANSWER_GRADING_INSTRUCTIONS.format(
            question=question,
            reference_answer=reference_answer,
            answer_text=answer_text,
        )
        reply = await self.runner.run(prompt)
        try:
            parsed = _ParsedShortAnswerGrade.model_validate_json(
                _extract_json_object(reply)
            )
        except _UNUSABLE_GRADING_REPLY_ERRORS as error:
            message = f"short-answer grading produced no verdict: {error}"
            raise AnswerGradingUnavailableError(message) from error
        return parsed.correct

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> EssayGradeProposal:
        """Propose an essay grade against the rubric via the model."""
        prompt = _ESSAY_GRADING_INSTRUCTIONS.format(
            question=question, rubric=rubric, answer_text=answer_text
        )
        reply = await self.runner.run(prompt)
        try:
            parsed = _ParsedEssayGrade.model_validate_json(_extract_json_object(reply))
        except _UNUSABLE_GRADING_REPLY_ERRORS as error:
            message = f"essay grading produced no proposal: {error}"
            raise AnswerGradingUnavailableError(message) from error
        return EssayGradeProposal(correct=parsed.correct, reasoning=parsed.reasoning)


def _normalized_answer(text: str) -> str:
    """Collapse whitespace and case so a strict match tolerates formatting only."""
    return " ".join(text.split()).casefold()


def matches_reference(reference_answer: str, answer_text: str) -> bool:
    """The strict-match fallback grade: normalized exact equality.

    >>> matches_reference("epoll", "  EPOLL ")
    True
    """
    return _normalized_answer(reference_answer) == _normalized_answer(answer_text)


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
    """One recall prompt: its question, grading payload, and SM-2 card state.

    `kind` discriminates the grading payload: multiple-choice rows carry
    `choices` + `correct_index`, short-answer rows `reference_answer`, essay
    rows `rubric`; the other payload columns stay NULL (and `choices` empty).
    """

    id: RecallPrompt.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    study_item_id: RecallPrompt.Col[UUID7] = Text()
    """The study item this prompt drills."""
    kind: RecallPrompt.Col[RecallPromptKind] = Text()
    question: RecallPrompt.Col[str] = Text()
    choices: RecallPrompt.Col[Json[list[str]]] = Text()
    """The multiple-choice options, as JSON; empty for other kinds."""
    correct_index: RecallPrompt.Col[int | None] = Integer(default=None, nullable=True)
    """Index into `choices` of the right answer; never sent to the client."""
    reference_answer: RecallPrompt.Col[str | None] = Text(default=None, nullable=True)
    """The short-answer key free text grades against; never sent to the client."""
    rubric: RecallPrompt.Col[str | None] = Text(default=None, nullable=True)
    """What a passing essay must cover; revealed only at the confirm-grade step."""
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
    selected_index: RecallAnswer.Col[int | None] = Integer(default=None, nullable=True)
    """The chosen option for a multiple-choice answer; NULL for free text."""
    answer_text: RecallAnswer.Col[str | None] = Text(default=None, nullable=True)
    """The free-text answer for short-answer and essay prompts; NULL for choices."""
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
class PromptAnswer:
    """One submitted answer, carrying the input for whichever kind it targets.

    Multiple choice sets `selected_index`; short answer sets `answer_text`;
    essay sets `answer_text` plus `confirmed_correct` — the grade the human
    confirmed after seeing the model's proposal (ADR 0004). `response_ms`
    always rides along to refine the SM-2 quality of a correct answer.
    """

    response_ms: int
    selected_index: int | None = None
    answer_text: str | None = None
    confirmed_correct: bool | None = None


@dataclass(frozen=True, slots=True)
class RecallModelSteps:
    """The model-backed collaborators of Recall, injected as one seam.

    `generator` distils a transcript into a study item (starting Recall);
    `grader` judges free-text answers (answering). The grader may be absent:
    short answers then fall back to the strict reference match and essay
    proposals come back empty — scheduling itself never needs a model.
    """

    generator: StudyItemGenerator
    grader: AnswerGrader | None = None


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
    """Reject a distillation with no prompts, an unknown kind, or a missing payload."""
    if not generated.prompts:
        message = "a study item requires at least one recall prompt"
        raise InvalidPromptError(message)
    for prompt in generated.prompts:
        # Widened to `str` so the unmatched-kind guard survives values the type
        # system says cannot happen (a lying caller): a kind this dispatch does
        # not recognise must be rejected, never validated as some other kind.
        kind: str = prompt.kind
        if kind == "multiple_choice":
            _validate_multiple_choice(prompt)
        elif kind == "short_answer":
            if not (prompt.reference_answer or "").strip():
                message = "a short-answer prompt requires a reference answer"
                raise InvalidPromptError(message)
        elif kind == "essay":
            if not (prompt.rubric or "").strip():
                message = "an essay prompt requires a rubric"
                raise InvalidPromptError(message)
        else:
            message = f"unknown recall prompt kind {kind!r}"
            raise InvalidPromptError(message)


def _validate_multiple_choice(prompt: GeneratedPrompt) -> None:
    """Reject a multiple-choice prompt with too few choices or a bad answer key."""
    if len(prompt.choices) < _MIN_CHOICES:
        message = "a multiple-choice prompt requires at least two choices"
        raise InvalidPromptError(message)
    if prompt.correct_index is None or not 0 <= prompt.correct_index < len(
        prompt.choices
    ):
        message = (
            f"correct_index {prompt.correct_index} is outside the "
            f"{len(prompt.choices)} choices"
        )
        raise InvalidPromptError(message)


class RecallService:
    """Capability surface for the Recall tethering path, over a snekql database.

    Starting Recall distils a transcript (a model-backed step, injected) into a
    loose Memory plus its prompts; answering reduces every kind to a
    `(correct, response_ms)` pair — deterministically for multiple choice, via
    the injected grader (with a strict-match fallback) for short answers, and
    from the human-confirmed grade for essays — then reschedules with pure SM-2
    math; learning the last prompt completes the study item and tethers its
    Memory through the shared `MemoryService` — so the second gate reuses the
    one Memory lifecycle rather than a parallel one.
    """

    def __init__(
        self,
        database: Database,
        memory_service: MemoryService,
        models: RecallModelSteps,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.memory_service: MemoryService = memory_service
        self.generator: StudyItemGenerator = models.generator
        self.grader: AnswerGrader | None = models.grader
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
                                kind=generated_prompt.kind,
                                question=generated_prompt.question,
                                choices=generated_prompt.choices,
                                correct_index=generated_prompt.correct_index,
                                reference_answer=generated_prompt.reference_answer,
                                rubric=generated_prompt.rubric,
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
        answer: PromptAnswer,
        *,
        now: datetime,
        logger: Logger,
    ) -> AnswerOutcome:
        """Grade and reschedule a prompt, completing the study item when learned.

        The answer is graded per the prompt's kind — a selected choice against
        the answer key, free text against the reference answer (model-assisted,
        strict match when the model is unavailable), or an essay by the
        human-confirmed grade — reduced to an SM-2 quality with the response
        time, and applied to the card. The answer is recorded for audit. When
        this learns the study item's final prompt the item completes and its
        Memory tethers (the Recall gate); a miss simply reschedules, extending
        the overall effort. Tethering happens **only** on full completion.
        """
        correct = await self._grade(prompt, answer, logger=logger)
        quality = grade_answer(
            correct=correct, response_ms=answer.response_ms, kind=prompt.kind
        )
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
                            selected_index=answer.selected_index,
                            answer_text=answer.answer_text,
                            correct=correct,
                            response_ms=answer.response_ms,
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

    async def propose_essay_grade(
        self,
        prompt: RecallPrompt[Fetched],
        *,
        answer_text: str,
        logger: Logger,
    ) -> EssayGradeProposal:
        """Ask the model to propose an essay grade for the human to confirm.

        The proposal is advisory: nothing is recorded or rescheduled here, and
        the grade that reaches SM-2 is the one the human later confirms through
        `answer_prompt` (ADR 0004 — the model must not self-certify learning).
        When the model is unavailable the proposal is empty and the human
        grades unaided.
        """
        if prompt.kind != "essay":
            message = f"prompt {prompt.id} is {prompt.kind}, not an essay"
            raise InvalidAnswerError(message)
        if not answer_text.strip():
            message = "an essay grade proposal requires the essay text"
            raise InvalidAnswerError(message)
        if prompt.rubric is None:
            # An essay row is written with its rubric (`_validate_generated`);
            # one without it is corrupt, so raise rather than grade unaided
            # against nothing.
            message = f"essay prompt {prompt.id} is missing its rubric"
            raise InvalidPromptError(message)
        if self.grader is None:
            return EssayGradeProposal(correct=None, reasoning=None)
        try:
            proposal = await self.grader.propose_essay_grade(
                question=prompt.question,
                rubric=prompt.rubric,
                answer_text=answer_text,
            )
        except AnswerGradingUnavailableError:
            logger.warning(
                "Essay grading unavailable; the human grades unaided",
                prompt_id=str(prompt.id),
            )
            return EssayGradeProposal(correct=None, reasoning=None)
        _info(
            logger,
            "Essay grade proposed",
            prompt_id=str(prompt.id),
            proposed_correct=proposal.correct,
        )
        return proposal

    async def _grade(
        self,
        prompt: RecallPrompt[Fetched],
        answer: PromptAnswer,
        *,
        logger: Logger,
    ) -> bool:
        """Reduce an answer to its correctness per the prompt's kind."""
        # Widened to `str` so a corrupt row's kind hits the explicit rejection
        # below instead of falling through into another kind's grading path.
        kind: str = prompt.kind
        if kind == "multiple_choice":
            if answer.selected_index is None:
                message = "a multiple-choice prompt is answered by selected_index"
                raise InvalidAnswerError(message)
            if not 0 <= answer.selected_index < len(prompt.choices):
                message = (
                    f"selected_index {answer.selected_index} is outside the "
                    f"{len(prompt.choices)} choices"
                )
                raise InvalidAnswerError(message)
            return answer.selected_index == prompt.correct_index
        if kind not in ("short_answer", "essay"):
            message = f"prompt {prompt.id} has unknown kind {kind!r}"
            raise InvalidPromptError(message)
        if answer.answer_text is None or not answer.answer_text.strip():
            message = f"a {kind} prompt is answered by answer_text"
            raise InvalidAnswerError(message)
        if kind == "essay":
            if answer.confirmed_correct is None:
                message = (
                    "an essay answer requires confirmed_correct: the human "
                    "confirms the grade (the model only proposes one)"
                )
                raise InvalidAnswerError(message)
            return answer.confirmed_correct
        return await self._grade_short_answer(
            prompt, answer_text=answer.answer_text, logger=logger
        )

    async def _grade_short_answer(
        self,
        prompt: RecallPrompt[Fetched],
        *,
        answer_text: str,
        logger: Logger,
    ) -> bool:
        """Grade free text via the model, strict-matching when it is unavailable."""
        reference_answer = prompt.reference_answer
        if reference_answer is None:
            message = f"prompt {prompt.id} has no reference answer to grade against"
            raise InvalidAnswerError(message)
        if self.grader is not None:
            try:
                # ADR 0004 boundary: this model verdict flows straight into
                # SM-2 (and, on the final card, completion → tethering). That
                # is a deliberate, scoped exception (issue #131): a short
                # answer has a strict factual key (`reference_answer`) the
                # model merely fuzzy-matches against, so the human still
                # authored the recalled fact. Essays, whose open-ended grade
                # is a judgement call, must instead be confirmed by the human
                # before their verdict reaches scheduling (`_grade` above).
                return await self.grader.grade_short_answer(
                    question=prompt.question,
                    reference_answer=reference_answer,
                    answer_text=answer_text,
                )
            except AnswerGradingUnavailableError:
                logger.warning(
                    "Short-answer grading unavailable; falling back to strict match",
                    prompt_id=str(prompt.id),
                )
        return matches_reference(reference_answer, answer_text)

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


def _recall_migrations() -> dict[str, str]:
    """The ordered Recall migration chain, one statement per migration.

    The `007_` bodies are the original scaffold (issue #20), frozen verbatim so
    the model classes can keep evolving without rewriting an already-applied
    migration; later shape changes are explicit `ALTER TABLE` steps.
    """
    migrations: dict[str, str] = {
        "007_create_study_item": (
            'CREATE TABLE "study_item" ('
            '"id" TEXT PRIMARY KEY NOT NULL, '
            '"memory_id" TEXT, "source_video_id" TEXT, "source_title" TEXT, '
            '"state" TEXT, '
            "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"completed_at" TEXT'
            ") STRICT"
        ),
        "007_create_index_ux_study_item_source_video_id": (
            'CREATE UNIQUE INDEX "ux_study_item_source_video_id" '
            'ON "study_item" ("source_video_id")'
        ),
        "007_create_index_ix_study_item_state": (
            'CREATE INDEX "ix_study_item_state" ON "study_item" ("state")'
        ),
        "007_create_recall_prompt": (
            'CREATE TABLE "recall_prompt" ('
            '"id" TEXT PRIMARY KEY NOT NULL, '
            '"study_item_id" TEXT, "kind" TEXT, "question" TEXT, "choices" TEXT, '
            '"correct_index" INTEGER, "repetitions" INTEGER, "ease_factor" REAL, '
            '"interval_days" INTEGER, "due_at" TEXT, '
            "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "007_create_index_ix_recall_prompt_study_item_id_due_at": (
            'CREATE INDEX "ix_recall_prompt_study_item_id_due_at" '
            'ON "recall_prompt" ("study_item_id", "due_at")'
        ),
        "007_create_recall_answer": (
            'CREATE TABLE "recall_answer" ('
            '"id" TEXT PRIMARY KEY NOT NULL, '
            '"prompt_id" TEXT, "selected_index" INTEGER, "correct" INTEGER, '
            '"response_ms" INTEGER, "quality" INTEGER, '
            "\"answered_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "007_create_index_ix_recall_answer_prompt_id": (
            'CREATE INDEX "ix_recall_answer_prompt_id" ON "recall_answer" ("prompt_id")'
        ),
    }
    # Prompt kinds beyond multiple choice (#131): the short-answer reference
    # key and essay rubric, plus the free-text answer in the audit log.
    migrations["010_recall_prompt_reference_answer"] = (
        'ALTER TABLE "recall_prompt" ADD COLUMN "reference_answer" TEXT'
    )
    migrations["010_recall_prompt_rubric"] = (
        'ALTER TABLE "recall_prompt" ADD COLUMN "rubric" TEXT'
    )
    migrations["010_recall_answer_answer_text"] = (
        'ALTER TABLE "recall_answer" ADD COLUMN "answer_text" TEXT'
    )
    return migrations


async def create_recall_schema(database: Database) -> None:
    """Create the study-item, recall-prompt, and answer tables on an initialized DB.

    Applied as its own ordered migrations after the earlier schemas (prefix
    `007_`, extended by `010_`). A snekql migration body runs exactly one
    statement, so each table, index, and column addition is its own ordered
    migration.
    """
    await database.migrate(_recall_migrations())
