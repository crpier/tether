"""User-facing service for studying and completing Recall items.

Recall owns distilled study material, prompts, scheduling, and answer history.
Learning progress does not write, promote, or otherwise gate Memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekok.result import Err, Ok
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.recall_generation import StudyItemGenerator, validate_generated_study_item
from tether.recall_grading import AnswerGrader, EssayGradeProposal, matches_reference
from tether.recall_schedule import (
    grade_answer,
    initial_schedule,
    is_learned,
    review_schedule,
)
from tether.recall_store import RecallAnswer, RecallPrompt, StudyItem, schedule_of
from tether.structured_logging import Logger


class StudyItemNotFoundError(Exception):
    """Raised when an operation targets a study item that does not exist."""


class RecallPromptNotFoundError(Exception):
    """Raised when an operation targets a Recall prompt that does not exist."""


class StudyItemExistsError(Exception):
    """Raised when a source already has a Recall study item."""


class TranscriptNotReadyError(Exception):
    """Raised when a source lacks the transcript required for Recall."""


class InvalidPromptError(Exception):
    """Raised when generated or persisted prompt data is invalid."""


class InvalidAnswerError(Exception):
    """Raised when an answer does not match its prompt's required input."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event with caller-supplied context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event with caller-supplied context."""
    logger.info(event, **context)


@dataclass(frozen=True, slots=True)
class PromptAnswer:
    """One submitted answer, carrying the input for whichever kind it targets.

    Multiple choice sets `selected_index`; short answer sets `answer_text`;
    essay sets `answer_text` plus `confirmed_correct` — the grade the human
    confirmed after seeing the model's proposal. `response_ms`
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
    """The result of answering a prompt and any Study-item completion."""

    prompt: RecallPrompt[Fetched]
    correct: bool
    quality: int
    completed: bool


@dataclass(frozen=True, slots=True)
class DuePrompt:
    """A prompt currently owed a review, with the study item it belongs to."""

    prompt: RecallPrompt[Fetched]
    study_item: StudyItem[Fetched]


class RecallService:
    """Capability surface for spaced learning over source-owned material.

    Starting Recall distils a transcript into a Study item and prompts. Answers
    reduce to `(correct, response_ms)`, reschedule through SM-2, and eventually
    complete that Study item. None of these transitions mutate Memory.
    """

    def __init__(
        self,
        database: Database,
        models: RecallModelSteps,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
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

        The transcript is distilled into learnings owned by the Study item and
        Recall prompts, each an SM-2 card due immediately. A source already under
        study conflicts rather than forking a second schedule.
        """
        with self.tracer.start_as_current_span(
            "RecallService.start_recall",
            attributes={"recall.source_video_id": source_video_id},
        ):
            _debug(logger, "Starting Recall", source_video_id=source_video_id)
            await self._require_absent(source_video_id)
            generation = await self.generator.generate(
                transcript=transcript, title=source_title
            )
            if isinstance(generation, Err):
                raise InvalidPromptError(generation.error.message)
            generated = generation.unwrap()
            validation = validate_generated_study_item(generated)
            if isinstance(validation, Err):
                raise InvalidPromptError(validation.error.message)

            async def _start_recall(tx: Transaction) -> StudyItem[Fetched]:
                study_item = await tx.execute(
                    insert(
                        StudyItem(
                            source_video_id=source_video_id,
                            source_title=source_title,
                            distilled_learnings=generated.distilled_learnings,
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

            async with self.database.transaction(mode="immediate") as tx:
                study_item = await _start_recall(tx)
        _info(
            logger,
            "Recall started",
            study_item_id=str(study_item.id),
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
        this learns the Study item's final prompt the item completes; a miss
        simply reschedules, extending the overall effort.
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

            async with self.database.transaction(mode="immediate") as tx:
                fresh_prompt, study_item, newly_complete = await _answer(tx)
        if newly_complete:
            _info(
                logger,
                "Recall completed",
                study_item_id=str(study_item.id),
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
        `answer_prompt`; the model must not self-certify learning.
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
        proposal_result = await self.grader.propose_essay_grade(
            question=prompt.question,
            rubric=prompt.rubric,
            answer_text=answer_text,
        )
        if isinstance(proposal_result, Err):
            logger.warning(
                "Essay grading unavailable; the human grades unaided",
                prompt_id=str(prompt.id),
            )
            return EssayGradeProposal(correct=None, reasoning=None)
        proposal = proposal_result.unwrap()
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
            grade = await self.grader.grade_short_answer(
                question=prompt.question,
                reference_answer=reference_answer,
                answer_text=answer_text,
            )
            if isinstance(grade, Ok):
                return grade.value
            logger.warning(
                "Short-answer grading unavailable; falling back to strict match",
                prompt_id=str(prompt.id),
            )
        return matches_reference(reference_answer, answer_text)

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
