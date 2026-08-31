"""Persistence models for Dreaming assimilation state and cursors."""

from __future__ import annotations

from typing import ClassVar, Literal, cast
from uuid import uuid7

from pydantic import UUID7, PositiveInt
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)

DreamRunKind = Literal["assimilation", "maintenance", "manual", "rebuild"]
"""Supported orchestration flavors for a Dream run."""

DreamRunStatus = Literal["queued", "running", "success", "no_op", "failed"]
"""Lifecycle state for one Dream execution request."""

DreamRunTerminalStatus = Literal["success", "no_op", "failed"]
"""Terminal states for a completed Dream run."""

DreamingMutationActor = Literal["dream", "human_external", "restore"]
"""Who caused this file mutation."""

DreamingMutationOperation = Literal["delete", "move", "restore", "write"]
"""Supported file mutation operations from the Dreaming path."""

DreamingMutationStatus = Literal["executed", "acknowledged", "failed"]
"""Mutation lifecycle states after notification attempts."""


class DreamingMutation[S = Pending](Model[S, "DreamingMutation[Fetched]"]):
    """One idempotent mutation attempt emitted by Dreaming execution."""

    id: sqlite.GenCol[UUID7] = Text(  # ty: ignore[invalid-assignment]
        primary_key=True,
        default_factory=uuid7,
    )
    run_id: sqlite.Col[UUID7] = Text()
    tool_call_id: sqlite.Col[str] = Text()
    actor: sqlite.Col[DreamingMutationActor] = Text()
    operation: sqlite.Col[DreamingMutationOperation] = Text()
    workspace_path: sqlite.Col[str] = Text()
    payload: sqlite.Col[str | None] = Text(default=None, nullable=True)
    before_content: sqlite.Col[str | None] = Text(default=None, nullable=True)
    after_content: sqlite.Col[str | None] = Text(default=None, nullable=True)
    status: sqlite.Col[DreamingMutationStatus] = Text(
        default=cast("DreamingMutationStatus", "executed")
    )
    attempts: sqlite.Col[int] = Integer(default=1)
    error: sqlite.Col[str | None] = Text(default=None, nullable=True)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(run_id, tool_call_id)]


class DreamingWorkspaceFile[S = Pending](Model[S, "DreamingWorkspaceFile[Fetched]"]):
    """Canonical current row for one workspace path and its latest bytes."""

    path: sqlite.GenCol[str] = Text(primary_key=True)
    content_hash: sqlite.Col[str] = Text()
    content: sqlite.Col[str | None] = Text(nullable=True)
    is_tombstone: sqlite.Col[int] = Integer(default=0)
    version: sqlite.Col[PositiveInt] = Integer(default=1)
    source_run_id: sqlite.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    source_tool_call_id: sqlite.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    actor: sqlite.Col[DreamingMutationActor | None] = Text(
        default=None,
        nullable=True,
    )
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class DreamConversationCursor[S = Pending](
    Model[S, "DreamConversationCursor[Fetched]"]
):
    """Per-conversation high-water mark for assimilated transcript messages."""

    conversation_id: sqlite.GenCol[UUID7] = Text(primary_key=True)
    last_assimilated_seq: sqlite.Col[int] = Integer(default=0)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class DreamRun[S = Pending](Model[S, "DreamRun[Fetched]"]):
    """One requested Dreaming consolidation run against one conversation."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    conversation_id: sqlite.Col[UUID7] = Text()
    kind: sqlite.Col[DreamRunKind] = Text()
    status: sqlite.Col[DreamRunStatus] = Text()
    evidence_start_seq: sqlite.Col[PositiveInt] = Integer()
    evidence_end_seq: sqlite.Col[PositiveInt] = Integer()
    attempts: sqlite.Col[PositiveInt] = Integer(default=1)
    error: sqlite.Col[str | None] = Text(default=None, nullable=True)
    completed_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class DreamMaintenanceProgress[S = Pending](
    Model[S, "DreamMaintenanceProgress[Fetched]"]
):
    """Last maintenance outcome for one workspace path."""

    path: sqlite.GenCol[str] = Text(primary_key=True)
    content_hash: sqlite.Col[str] = Text()
    maintained_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class HealthDreamRun[S = Pending](Model[S, "HealthDreamRun[Fetched]"]):
    """One Health consolidation run over a bounded summary version window.

    Bespoke sibling of DreamRun (ADR 0016): bounds are Health Connect source
    version ids per session type rather than transcript sequence numbers.
    """

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    status: sqlite.Col[DreamRunStatus] = Text()
    exercise_since_version_id: sqlite.Col[int] = Integer(default=0)
    exercise_through_version_id: sqlite.Col[int] = Integer(nullable=False)
    sleep_since_version_id: sqlite.Col[int] = Integer(default=0)
    sleep_through_version_id: sqlite.Col[int] = Integer(nullable=False)
    attempts: sqlite.Col[PositiveInt] = Integer(default=1)
    error: sqlite.Col[str | None] = Text(default=None, nullable=True)
    completed_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


_DREAM_MIGRATIONS = {
    # Original scaffold, frozen so later model additions do not rewrite migrations
    # that production has already applied.
    "001_create_dream_conversation_cursor": (
        'CREATE TABLE "dream_conversation_cursor" ('
        '"conversation_id" TEXT PRIMARY KEY NOT NULL, '
        '"last_assimilated_seq" INTEGER NOT NULL, '
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "002_create_dream_run": (
        'CREATE TABLE "dream_run" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "conversation_id" TEXT NOT NULL, '
        '"kind" TEXT NOT NULL, "status" TEXT NOT NULL, '
        '"evidence_start_seq" INTEGER NOT NULL, '
        '"evidence_end_seq" INTEGER NOT NULL, "attempts" INTEGER NOT NULL, '
        '"error" TEXT, "completed_at" TEXT, '
        '"created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "003_create_dreaming_mutation": (
        'CREATE TABLE "dreaming_mutation" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "run_id" TEXT NOT NULL, '
        '"tool_call_id" TEXT NOT NULL, "actor" TEXT NOT NULL, '
        '"operation" TEXT NOT NULL, "workspace_path" TEXT NOT NULL, '
        '"payload" TEXT, "status" TEXT NOT NULL, "attempts" INTEGER NOT NULL, '
        '"error" TEXT, "created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "004_create_index_ix_dreaming_mutation_run_id_tool_call_id": (
        'CREATE INDEX "ix_dreaming_mutation_run_id_tool_call_id" '
        'ON "dreaming_mutation" ("run_id", "tool_call_id")'
    ),
    "005_create_dreaming_workspace_file": (
        'CREATE TABLE "dreaming_workspace_file" ('
        '"path" TEXT PRIMARY KEY NOT NULL, "content_hash" TEXT NOT NULL, '
        '"content" TEXT, "is_tombstone" INTEGER NOT NULL, '
        '"version" INTEGER NOT NULL, "source_run_id" TEXT, '
        '"source_tool_call_id" TEXT, "actor" TEXT, '
        '"created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "006_dreaming_mutation_before_content": (
        'ALTER TABLE "dreaming_mutation" ADD COLUMN "before_content" TEXT'
    ),
    "007_dreaming_mutation_after_content": (
        'ALTER TABLE "dreaming_mutation" ADD COLUMN "after_content" TEXT'
    ),
    "008_create_health_dream_run": (
        'CREATE TABLE "health_dream_run" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "status" TEXT NOT NULL, '
        '"exercise_since_version_id" INTEGER NOT NULL DEFAULT 0, '
        '"exercise_through_version_id" INTEGER NOT NULL, '
        '"sleep_since_version_id" INTEGER NOT NULL DEFAULT 0, '
        '"sleep_through_version_id" INTEGER NOT NULL, '
        '"attempts" INTEGER NOT NULL, "error" TEXT, "completed_at" TEXT, '
        '"created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "009_create_dream_maintenance_progress": (
        'CREATE TABLE "dream_maintenance_progress" ('
        '"path" TEXT PRIMARY KEY NOT NULL, "content_hash" TEXT NOT NULL, '
        '"maintained_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
}
"""Ordered startup migrations for Dreaming state tables."""


async def create_dreaming_schema(database: Database) -> None:
    """Create Dreaming persistence tables on an initialized database."""
    await database.migrate(_DREAM_MIGRATIONS)


__all__ = [
    "DreamConversationCursor",
    "DreamMaintenanceProgress",
    "DreamRun",
    "DreamRunKind",
    "DreamRunStatus",
    "DreamRunTerminalStatus",
    "DreamingMutation",
    "DreamingMutationActor",
    "DreamingMutationOperation",
    "DreamingMutationStatus",
    "DreamingWorkspaceFile",
    "HealthDreamRun",
    "create_dreaming_schema",
]
