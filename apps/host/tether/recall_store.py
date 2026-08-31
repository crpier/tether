"""Persisted Recall models and schema migrations."""

from __future__ import annotations

from typing import ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7, Json
from snekql import sqlite
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
    """Distilled source material progressing through Recall."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    source_video_id: sqlite.Col[str] = Text(unique=True)
    source_title: sqlite.Col[str] = Text()
    distilled_learnings: sqlite.Col[str] = Text()
    state: sqlite.Col[StudyItemState] = Text()
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    completed_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)

    __indexes__: ClassVar = [Index(state)]


class RecallPrompt[S = Pending](Model[S, "RecallPrompt[Fetched]"]):
    """A persisted Recall prompt and its independent scheduling state."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    study_item_id: sqlite.Col[UUID7] = Text()
    kind: sqlite.Col[RecallPromptKind] = Text()
    question: sqlite.Col[str] = Text()
    choices: sqlite.Col[Json[list[str]]] = Text()
    correct_index: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    reference_answer: sqlite.Col[str | None] = Text(default=None, nullable=True)
    rubric: sqlite.Col[str | None] = Text(default=None, nullable=True)
    repetitions: sqlite.Col[int] = Integer(default=0)
    ease_factor: sqlite.Col[float] = Real(default=INITIAL_EASE_FACTOR)
    interval_days: sqlite.Col[int] = Integer(default=0)
    due_at: sqlite.Col[UtcDatetime] = Text()
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(study_item_id, due_at)]


class RecallAnswer[S = Pending](Model[S, "RecallAnswer[Fetched]"]):
    """An append-only audit record for one answered prompt."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    prompt_id: sqlite.Col[UUID7] = Text()
    selected_index: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    answer_text: sqlite.Col[str | None] = Text(default=None, nullable=True)
    correct: sqlite.Col[bool] = Integer()
    response_ms: sqlite.Col[int] = Integer()
    quality: sqlite.Col[int] = Integer()
    answered_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

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
            '"source_video_id" TEXT, "source_title" TEXT, '
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
    # Recall now owns its distilled study material and no longer participates in
    # the removed loose/tethered Memory lifecycle. The cutover is destructive:
    # old Memory links are discarded rather than translated into current Memory.
    migrations["017_recall_add_distilled_learnings"] = (
        'ALTER TABLE "study_item" ADD COLUMN "distilled_learnings" '
        "TEXT NOT NULL DEFAULT ''"
    )
    migrations["018_recall_drop_memory_id"] = (
        'UPDATE "study_item" SET "id" = "id" WHERE 0'
    )
    return migrations


async def create_recall_schema(database: Database) -> None:
    """Create or migrate the study-item, prompt, and answer tables."""
    await database.migrate(_recall_migrations())
