"""Behavior tests for the Recall service layer (study items + answering).

These drive the `RecallService` seam directly against a real in-memory SQLite
database, a real `MemoryService`, and a controlled fake generator — no model, no
HTTP. They assert the load-bearing Recall behavior: a source becomes a loose
study-item Memory with due prompts, answers grade and reschedule deterministically,
and a study item tethers its Memory **only** on full completion. Driving
controlled answers and timestamps keeps it all model-free (issue #20).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid7

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched, select, update
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryService,
    create_memory_schema,
)
from tether.recall import (
    AnswerGrader,
    AnswerGradingUnavailableError,
    EssayGradeProposal,
    GeneratedPrompt,
    GeneratedStudyItem,
    InvalidAnswerError,
    InvalidPromptError,
    PromptAnswer,
    RecallModelSteps,
    RecallPrompt,
    RecallPromptKind,
    RecallService,
    StudyItem,
    StudyItemExistsError,
    create_recall_schema,
)

LOGGER: Logger = structlog.stdlib.get_logger("test.recall_service")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.recall_service")


class FakeGenerator:
    """A controlled `StudyItemGenerator` returning a fixed distillation.

    The schedule and completion logic is what the tests exercise, so the model
    step is replaced by a deterministic stub: it ignores the transcript and
    returns the learnings and prompts it was constructed with.
    """

    def __init__(self, distilled: GeneratedStudyItem) -> None:
        self.distilled: GeneratedStudyItem = distilled
        self.calls: int = 0

    async def generate(self, *, transcript: str, title: str) -> GeneratedStudyItem:
        _ = (transcript, title)
        self.calls += 1
        return self.distilled


def one_prompt() -> GeneratedStudyItem:
    """A distillation with a single multiple-choice prompt."""
    return GeneratedStudyItem(
        distilled_learnings="Async IO multiplexes one thread over many waits.",
        prompts=[
            GeneratedPrompt(
                question="What does async IO multiplex?",
                choices=["One thread over many waits", "Many threads", "Processes"],
                correct_index=0,
            )
        ],
    )


def two_prompts() -> GeneratedStudyItem:
    """A distillation with two multiple-choice prompts."""
    return GeneratedStudyItem(
        distilled_learnings="Two facts about async IO.",
        prompts=[
            GeneratedPrompt(question="Q1?", choices=["a", "b"], correct_index=0),
            GeneratedPrompt(question="Q2?", choices=["a", "b"], correct_index=1),
        ],
    )


def one_short_answer() -> GeneratedStudyItem:
    """A distillation with a single short-answer prompt."""
    return GeneratedStudyItem(
        distilled_learnings="The event loop is built on epoll.",
        prompts=[
            GeneratedPrompt(
                question="Name the syscall behind the event loop.",
                kind="short_answer",
                reference_answer="epoll",
            )
        ],
    )


def one_essay() -> GeneratedStudyItem:
    """A distillation with a single essay prompt."""
    return GeneratedStudyItem(
        distilled_learnings="Event loops schedule coroutines cooperatively.",
        prompts=[
            GeneratedPrompt(
                question="Explain how an event loop schedules coroutines.",
                kind="essay",
                rubric="Mentions readiness, callbacks, and cooperative yielding.",
            )
        ],
    )


class FakeGrader:
    """A controlled `AnswerGrader` with scripted verdicts and call recording."""

    def __init__(
        self,
        *,
        short_answer_correct: bool | None = None,
        proposal: EssayGradeProposal | None = None,
    ) -> None:
        self.short_answer_correct: bool | None = short_answer_correct
        self.proposal: EssayGradeProposal | None = proposal
        self.short_answer_calls: list[tuple[str, str, str]] = []
        self.proposal_calls: list[tuple[str, str, str]] = []

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> bool:
        self.short_answer_calls.append((question, reference_answer, answer_text))
        if self.short_answer_correct is None:
            message = "grader scripted as unavailable"
            raise AnswerGradingUnavailableError(message)
        return self.short_answer_correct

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> EssayGradeProposal:
        self.proposal_calls.append((question, rubric, answer_text))
        if self.proposal is None:
            message = "grader scripted as unavailable"
            raise AnswerGradingUnavailableError(message)
        return self.proposal


class RecallFixture:
    """A wired Recall service with its collaborators, for behavior assertions."""

    def __init__(
        self,
        *,
        service: RecallService,
        memory_service: MemoryService,
        generator: FakeGenerator,
        database: Database,
    ) -> None:
        self.service: RecallService = service
        self.memory_service: MemoryService = memory_service
        self.generator: FakeGenerator = generator
        self.database: Database = database

    async def start(
        self, *, video_id: str = "v1", title: str = "Async IO", now: datetime = NOW
    ) -> StudyItem[Fetched]:
        """Start Recall for a source with the fixture's logger."""
        return await self.service.start_recall(
            source_video_id=video_id,
            source_title=title,
            transcript="(transcript)",
            now=now,
            logger=LOGGER,
        )

    async def memory(self, study_item: StudyItem[Fetched]) -> Memory[Fetched]:
        """Fetch the distilled-learnings Memory behind a study item."""
        async with self.database.transaction() as tx:
            memory = await tx.fetch_one_or_none(
                select(Memory).where(Memory.id.eq(study_item.memory_id))
            )
        assert memory is not None
        return memory

    async def study_item(self, study_item_id: UUID7) -> StudyItem[Fetched]:
        """Re-fetch a study item row for state assertions."""
        async with self.database.transaction() as tx:
            item = await tx.fetch_one_or_none(
                select(StudyItem).where(StudyItem.id.eq(study_item_id))
            )
        assert item is not None
        return item


@fixture
async def recall_fixture(
    generator: FakeGenerator, grader: AnswerGrader | None = None
) -> AsyncGenerator[RecallFixture]:
    """A fresh Recall service over an isolated DB, KB dir, and fake generator."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_recall_schema(db)
    async with TemporaryDirectory() as kb_dir:
        memory_service = MemoryService(
            database=db,
            kb_service=KnowledgeBaseService(kb_root=Path(kb_dir)),
            tracer=noop_tracer(),
        )
        service = RecallService(
            database=db,
            memory_service=memory_service,
            models=RecallModelSteps(generator=generator, grader=grader),
            tracer=noop_tracer(),
        )
        yield RecallFixture(
            service=service,
            memory_service=memory_service,
            generator=generator,
            database=db,
        )
    await db.close()


async def drive_to_learned(
    fixture: RecallFixture, prompt: RecallPrompt[Fetched]
) -> RecallPrompt[Fetched]:
    """Answer a prompt correctly across rounds until it graduates to learned."""
    current = prompt
    now = current.due_at
    for _ in range(10):
        outcome = await fixture.service.answer_prompt(
            current,
            PromptAnswer(selected_index=current.correct_index, response_ms=1_000),
            now=now,
            logger=LOGGER,
        )
        current = outcome.prompt
        now = current.due_at + timedelta(seconds=1)
        if outcome.completed or current.repetitions >= 3:
            return current
    return current


# --- start_recall ---


@test()
async def start_recall_creates_a_loose_memory_with_due_prompts() -> None:
    """Starting Recall distils a loose Memory and a prompt due immediately."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))

    study_item = await fixture.start()

    assert_eq(study_item.state, "studying")
    memory = await fixture.memory(study_item)
    assert_eq(memory.content, "Async IO multiplexes one thread over many waits.")
    assert_is_none(memory.tethered_at)
    assert_eq(memory.provenance, {"kind": "youtube"})
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)
    assert_eq(len(due), 1)
    assert_eq(due[0].prompt.question, "What does async IO multiplex?")


@test()
async def list_due_prompts_caps_rows_at_the_limit() -> None:
    """`limit` bounds the due list to the soonest-due prompts."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(two_prompts())))
    _ = await fixture.start()

    due = await fixture.service.list_due_prompts(NOW, limit=1, logger=LOGGER)

    assert_eq(len(due), 1)


@test()
async def start_recall_rejects_a_source_already_under_study() -> None:
    """A source becomes a study item at most once."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start(video_id="v1")

    with assert_raises(StudyItemExistsError):
        _ = await fixture.start(video_id="v1")


@test()
async def start_recall_rejects_a_distillation_with_no_prompts() -> None:
    """A study item with no prompts can never be recalled, so it is rejected."""
    empty = GeneratedStudyItem(distilled_learnings="x", prompts=[])
    fixture = await load_fixture(recall_fixture(FakeGenerator(empty)))

    with assert_raises(InvalidPromptError):
        _ = await fixture.start()


# --- answering and rescheduling ---


@test()
async def answering_correctly_pushes_the_prompt_out_of_the_due_list() -> None:
    """A correct answer reschedules the prompt into the future, off today's queue."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    study_item = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(selected_index=due[0].prompt.correct_index, response_ms=1_000),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    still_due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)
    assert_eq(len(still_due), 0)
    _ = study_item


@test()
async def answering_incorrectly_keeps_the_prompt_due_soon() -> None:
    """A wrong answer resets the card to a one-day relearning step."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)
    correct_index = due[0].prompt.correct_index
    assert correct_index is not None
    wrong_index = (correct_index + 1) % len(due[0].prompt.choices)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(selected_index=wrong_index, response_ms=1_000),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, False)
    assert_eq(outcome.completed, False)
    assert_eq(outcome.prompt.repetitions, 0)
    assert_eq(outcome.prompt.due_at, NOW + timedelta(days=1))


@test()
async def answering_rejects_an_out_of_range_choice() -> None:
    """An answer outside the prompt's choices is a domain error, not a miss."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    with assert_raises(InvalidAnswerError):
        _ = await fixture.service.answer_prompt(
            due[0].prompt,
            PromptAnswer(selected_index=99, response_ms=1_000),
            now=NOW,
            logger=LOGGER,
        )


# --- completion and the tether gate ---


@test()
async def full_completion_tethers_the_distilled_learnings_memory() -> None:
    """Learning every prompt completes the study item and tethers its Memory."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    study_item = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    final = await drive_to_learned(fixture, due[0].prompt)

    assert_eq(final.repetitions >= 3, True)
    refreshed = await fixture.study_item(study_item.id)
    assert_eq(refreshed.state, "completed")
    assert_is_not_none(refreshed.completed_at)
    memory = await fixture.memory(study_item)
    assert_is_not_none(memory.tethered_at)


@test()
async def partial_progress_does_not_tether_the_memory() -> None:
    """With one prompt still un-learned the Memory stays loose (tether is all-or-nothing)."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(two_prompts())))
    study_item = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    # Fully learn only the first prompt; leave the second untouched.
    _ = await drive_to_learned(fixture, due[0].prompt)

    refreshed = await fixture.study_item(study_item.id)
    assert_eq(refreshed.state, "studying")
    memory = await fixture.memory(study_item)
    assert_is_none(memory.tethered_at)


@test()
async def completion_tolerates_a_memory_already_tethered_by_review() -> None:
    """A human Review may tether first; completion still settles, without re-tethering."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    study_item = await fixture.start()
    memory = await fixture.memory(study_item)
    _ = await fixture.memory_service.tether(memory, logger=LOGGER)
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    final = await drive_to_learned(fixture, due[0].prompt)

    assert_eq(final.repetitions >= 3, True)
    refreshed = await fixture.study_item(study_item.id)
    assert_eq(refreshed.state, "completed")


# --- short-answer grading (#131) ---


@test()
async def a_short_answer_is_graded_by_the_model_grader() -> None:
    """A short answer defers to the model grader's verdict, not string equality."""
    grader = FakeGrader(short_answer_correct=True)
    fixture = await load_fixture(
        recall_fixture(FakeGenerator(one_short_answer()), grader)
    )
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(
            answer_text="the loop is built on the epoll syscall", response_ms=1_000
        ),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    assert_eq(
        grader.short_answer_calls,
        [
            (
                "Name the syscall behind the event loop.",
                "epoll",
                "the loop is built on the epoll syscall",
            )
        ],
    )


@test()
async def a_short_answer_falls_back_to_strict_match_when_the_grader_is_unavailable() -> (
    None
):
    """With no model verdict, a normalized exact match against the reference passes."""
    grader = FakeGrader(short_answer_correct=None)  # scripted unavailable
    fixture = await load_fixture(
        recall_fixture(FakeGenerator(one_short_answer()), grader)
    )
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(answer_text="  EPOLL ", response_ms=1_000),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    assert_eq(len(grader.short_answer_calls), 1)


@test()
async def a_short_answer_without_any_grader_strict_matches() -> None:
    """A service wired without a grader still grades by strict reference match."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_short_answer())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(answer_text="select", response_ms=1_000),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, False)


@test()
async def a_short_answer_requires_answer_text() -> None:
    """Answering a short-answer prompt with a choice index is a domain error."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_short_answer())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    with assert_raises(InvalidAnswerError):
        _ = await fixture.service.answer_prompt(
            due[0].prompt,
            PromptAnswer(selected_index=0, response_ms=1_000),
            now=NOW,
            logger=LOGGER,
        )


@test()
async def a_multiple_choice_prompt_requires_a_selected_index() -> None:
    """Answering a multiple-choice prompt with free text is a domain error."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    with assert_raises(InvalidAnswerError):
        _ = await fixture.service.answer_prompt(
            due[0].prompt,
            PromptAnswer(answer_text="one thread over many waits", response_ms=1_000),
            now=NOW,
            logger=LOGGER,
        )


# --- essay grading: the human confirms (#131, ADR 0004) ---


@test()
async def an_essay_grade_comes_from_the_human_confirmation() -> None:
    """The SM-2 input for an essay is the human's confirmed grade, not the model's."""
    grader = FakeGrader(proposal=EssayGradeProposal(correct=False, reasoning="weak"))
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay()), grader))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(
            answer_text="Readiness polling plus cooperative yields.",
            confirmed_correct=True,
            response_ms=1_000,
        ),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    # Answering never re-consults the model: the human's confirmation is final.
    assert_eq(grader.proposal_calls, [])


@test()
async def an_essay_answer_requires_the_confirmed_grade() -> None:
    """An essay answer without a human-confirmed grade is a domain error."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    with assert_raises(InvalidAnswerError):
        _ = await fixture.service.answer_prompt(
            due[0].prompt,
            PromptAnswer(
                answer_text="Readiness polling plus cooperative yields.",
                response_ms=1_000,
            ),
            now=NOW,
            logger=LOGGER,
        )


@test()
async def propose_essay_grade_returns_the_model_proposal() -> None:
    """Proposing a grade hands back the model's verdict for the human to confirm."""
    grader = FakeGrader(
        proposal=EssayGradeProposal(correct=True, reasoning="Covers the rubric.")
    )
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay()), grader))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    proposal = await fixture.service.propose_essay_grade(
        due[0].prompt,
        answer_text="Readiness polling plus cooperative yields.",
        logger=LOGGER,
    )

    assert_eq(proposal.correct, True)
    assert_eq(proposal.reasoning, "Covers the rubric.")
    assert_eq(len(grader.proposal_calls), 1)


@test()
async def propose_essay_grade_degrades_to_no_proposal_when_unavailable() -> None:
    """With no model, the proposal is empty and the human grades unaided."""
    grader = FakeGrader(proposal=None)  # scripted unavailable
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay()), grader))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    proposal = await fixture.service.propose_essay_grade(
        due[0].prompt, answer_text="An answer.", logger=LOGGER
    )

    assert_is_none(proposal.correct)
    assert_is_none(proposal.reasoning)


@test()
async def propose_essay_grade_rejects_non_essay_prompts() -> None:
    """Only essays carry a rubric to grade against."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    with assert_raises(InvalidAnswerError):
        _ = await fixture.service.propose_essay_grade(
            due[0].prompt, answer_text="An answer.", logger=LOGGER
        )


@test()
async def start_recall_rejects_a_short_answer_without_a_reference() -> None:
    """A short-answer prompt with no reference answer can never be graded."""
    malformed = GeneratedStudyItem(
        distilled_learnings="x",
        prompts=[GeneratedPrompt(question="q", kind="short_answer")],
    )
    fixture = await load_fixture(recall_fixture(FakeGenerator(malformed)))

    with assert_raises(InvalidPromptError):
        _ = await fixture.start()


@test()
async def start_recall_rejects_an_essay_without_a_rubric() -> None:
    """An essay prompt with no rubric can never be confirm-graded."""
    malformed = GeneratedStudyItem(
        distilled_learnings="x",
        prompts=[GeneratedPrompt(question="q", kind="essay")],
    )
    fixture = await load_fixture(recall_fixture(FakeGenerator(malformed)))

    with assert_raises(InvalidPromptError):
        _ = await fixture.start()


async def clear_rubric(
    fixture: RecallFixture, prompt_id: UUID7
) -> RecallPrompt[Fetched]:
    """NULL out a stored essay's rubric and re-fetch it.

    The domain never writes such a row — `_validate_generated` rejects it — so
    this reaches under the service to prove a corrupt row is refused rather
    than silently graded against an empty rubric.
    """
    async with fixture.database.transaction() as tx:
        _ = await tx.execute(
            update(RecallPrompt)
            .set(RecallPrompt.rubric.to(None))
            .where(RecallPrompt.id.eq(prompt_id))
        )
    return await fixture.service.fetch_prompt(prompt_id)


@test()
async def start_recall_rejects_an_unknown_prompt_kind() -> None:
    """A generated prompt of an unrecognised kind is refused, never stored."""
    malformed = GeneratedStudyItem(
        distilled_learnings="x",
        prompts=[
            GeneratedPrompt(question="q", kind=cast("RecallPromptKind", "flashcard"))
        ],
    )
    fixture = await load_fixture(recall_fixture(FakeGenerator(malformed)))

    with assert_raises(InvalidPromptError):
        _ = await fixture.start()


@test()
async def grading_an_unmatched_prompt_kind_is_rejected() -> None:
    """The grading dispatch refuses an unrecognised kind, never falls through.

    snekql validates `kind` on write, fetch, and model construction, so a
    corrupt row cannot reach the service through the database; this stubs the
    prompt to prove the dispatch itself rejects an unmatched kind rather than
    grading it as some other kind.
    """
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    corrupt = cast(
        "RecallPrompt[Fetched]",
        SimpleNamespace(id=uuid7(), kind="flashcard", choices=[]),
    )

    with assert_raises(InvalidPromptError):
        _ = await fixture.service._grade(
            corrupt,
            PromptAnswer(answer_text="anything", response_ms=1_000),
            logger=LOGGER,
        )


@test()
async def a_slow_correct_essay_grades_the_fixed_free_text_quality() -> None:
    """Essay quality comes from the confirmed grade alone, not composition time."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(
            answer_text="Readiness polling plus cooperative yields.",
            confirmed_correct=True,
            response_ms=300_000,  # essays take minutes; never a hesitation signal
        ),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    assert_eq(outcome.quality, 4)


@test()
async def a_slow_correct_short_answer_is_not_penalised_for_typing_time() -> None:
    """Short-answer quality ignores response time (typing is not hesitation)."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_short_answer())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        PromptAnswer(answer_text="epoll", response_ms=120_000),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(outcome.correct, True)
    assert_eq(outcome.quality, 4)


@test()
async def propose_essay_grade_rejects_an_essay_row_missing_its_rubric() -> None:
    """An essay row without a rubric is corrupt: raise, never grade against ''."""
    grader = FakeGrader(proposal=EssayGradeProposal(correct=True, reasoning="ok"))
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_essay()), grader))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)
    corrupt = await clear_rubric(fixture, due[0].prompt.id)

    with assert_raises(InvalidPromptError):
        _ = await fixture.service.propose_essay_grade(
            corrupt, answer_text="An essay.", logger=LOGGER
        )
    assert_eq(grader.proposal_calls, [])


@test()
async def a_mixed_kind_study_item_completes_and_tethers() -> None:
    """MC, short-answer, and essay cards all feed the same SM-2 gate to tether."""
    mixed = GeneratedStudyItem(
        distilled_learnings="Mixed learnings.",
        prompts=[
            GeneratedPrompt(question="MC?", choices=["a", "b"], correct_index=1),
            GeneratedPrompt(
                question="SA?", kind="short_answer", reference_answer="epoll"
            ),
            GeneratedPrompt(question="ES?", kind="essay", rubric="Covers X."),
        ],
    )
    fixture = await load_fixture(recall_fixture(FakeGenerator(mixed)))
    study_item = await fixture.start()

    for _ in range(4):
        due = await fixture.service.list_due_prompts(
            NOW + timedelta(days=365), logger=LOGGER
        )
        for entry in due:
            kind = entry.prompt.kind
            _ = await fixture.service.answer_prompt(
                entry.prompt,
                PromptAnswer(
                    selected_index=(
                        entry.prompt.correct_index
                        if kind == "multiple_choice"
                        else None
                    ),
                    answer_text="epoll" if kind != "multiple_choice" else None,
                    confirmed_correct=True if kind == "essay" else None,
                    response_ms=1_000,
                ),
                now=entry.prompt.due_at,
                logger=LOGGER,
            )

    refreshed = await fixture.study_item(study_item.id)
    assert_eq(refreshed.state, "completed")
    memory = await fixture.memory(study_item)
    assert_is_not_none(memory.tethered_at)


@test()
async def completed_study_items_drop_off_the_due_list() -> None:
    """A completed study item's prompts no longer appear as outstanding."""
    fixture = await load_fixture(recall_fixture(FakeGenerator(one_prompt())))
    _ = await fixture.start()
    due = await fixture.service.list_due_prompts(NOW, logger=LOGGER)
    final = await drive_to_learned(fixture, due[0].prompt)

    later = final.due_at + timedelta(days=365)
    remaining = await fixture.service.list_due_prompts(later, logger=LOGGER)

    assert_eq(len(remaining), 0)
