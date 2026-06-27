"""Behavior tests for the Recall service layer (study items + answering).

These drive the `RecallService` seam directly against a real in-memory SQLite
database, a real `MemoryService`, and a controlled fake generator — no model, no
HTTP. They assert the load-bearing Recall behavior: a source becomes a loose
study-item Memory with due prompts, answers grade and reschedule deterministically,
and a study item tethers its Memory **only** on full completion. Driving
controlled answers and timestamps keeps it all model-free (issue #20).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched, select
from snektest import (
    AsyncFixture,
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
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
    GeneratedPrompt,
    GeneratedStudyItem,
    InvalidAnswerError,
    InvalidPromptError,
    RecallPrompt,
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


async def recall_fixture(generator: FakeGenerator) -> AsyncFixture[RecallFixture]:
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
            generator=generator,
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
            selected_index=current.correct_index,
            response_ms=1_000,
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
        selected_index=due[0].prompt.correct_index,
        response_ms=1_000,
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
    wrong_index = (due[0].prompt.correct_index + 1) % len(due[0].prompt.choices)

    outcome = await fixture.service.answer_prompt(
        due[0].prompt,
        selected_index=wrong_index,
        response_ms=1_000,
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
            selected_index=99,
            response_ms=1_000,
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
