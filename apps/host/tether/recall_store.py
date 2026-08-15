"""Persisted Recall models and schema migrations."""

from __future__ import annotations

from typing import ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7, Json
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
    UtcDatetime,
)

from tether.recall_schedule import (
    INITIAL_EASE_FACTOR,
    RecallPromptKind,
    RecallSchedule,
)

type StudyItemState = Literal["studying", "completed"]
"""A study item's lifecycle while prompts are active and after graduation."""


class StudyItem[S = Pending](Model[S, "StudyItem[Fetched]"]):
    """A loose Memory under Recall, paired with its source."""

    id: StudyItem.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    memory_id: StudyItem.Col[UUID7] = Text()
    source_video_id: StudyItem.Col[str] = Text(unique=True)
    source_title: StudyItem.Col[str] = Text()
    state: StudyItem.Col[StudyItemState] = Text()
    created_at: StudyItem.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: StudyItem.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    completed_at: StudyItem.Col[UtcDatetime | None] = Text(default=None, nullable=True)

    __indexes__: ClassVar = [Index(state)]


class RecallPrompt[S = Pending](Model[S, "RecallPrompt[Fetched]"]):
    """A persisted Recall prompt and its independent scheduling state."""

    id: RecallPrompt.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    study_item_id: RecallPrompt.Col[UUID7] = Text()
    kind: RecallPrompt.Col[RecallPromptKind] = Text()
    question: RecallPrompt.Col[str] = Text()
    choices: RecallPrompt.Col[Json[list[str]]] = Text()
    correct_index: RecallPrompt.Col[int | None] = Integer(default=None, nullable=True)
    reference_answer: RecallPrompt.Col[str | None] = Text(default=None, nullable=True)
    rubric: RecallPrompt.Col[str | None] = Text(default=None, nullable=True)
    repetitions: RecallPrompt.Col[int] = Integer(default=0)
    ease_factor: RecallPrompt.Col[float] = Real(default=INITIAL_EASE_FACTOR)
    interval_days: RecallPrompt.Col[int] = Integer(default=0)
    due_at: RecallPrompt.Col[UtcDatetime] = Text()
    created_at: RecallPrompt.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: RecallPrompt.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(study_item_id, due_at)]


class RecallAnswer[S = Pending](Model[S, "RecallAnswer[Fetched]"]):
    """An append-only audit record for one answered prompt."""

    id: RecallAnswer.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    prompt_id: RecallAnswer.Col[UUID7] = Text()
    selected_index: RecallAnswer.Col[int | None] = Integer(default=None, nullable=True)
    answer_text: RecallAnswer.Col[str | None] = Text(default=None, nullable=True)
    correct: RecallAnswer.Col[bool] = Integer()
    response_ms: RecallAnswer.Col[int] = Integer()
    quality: RecallAnswer.Col[int] = Integer()
    answered_at: RecallAnswer.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(prompt_id)]


def schedule_of(prompt: RecallPrompt[Fetched]) -> RecallSchedule:
    """Project a stored prompt onto the pure scheduling value."""
    return RecallSchedule(
        due_at=prompt.due_at,
        ease_factor=prompt.ease_factor,
        interval_days=prompt.interval_days,
        repetitions=prompt.repetitions,
    )


def _recall_migrations() -> dict[str, str]:
    """The ordered Recall migration chain, one statement per migration.

    The `007_` bodies are the original scaffold, frozen verbatim so the model
    classes can keep evolving without rewriting an already-applied
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
    # Prompt kinds beyond multiple choice add the short-answer reference
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
    """Create or migrate the study-item, prompt, and answer tables."""
    await database.migrate(_recall_migrations())
