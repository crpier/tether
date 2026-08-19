"""Persistence models for Dreaming assimilation state and cursors."""

from __future__ import annotations

from typing import ClassVar, Literal, cast
from uuid import uuid7

from pydantic import UUID7, PositiveInt
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
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

DreamRunKind = Literal["assimilation", "maintenance", "manual"]
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

    id: DreamingMutation.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    run_id: DreamingMutation.Col[UUID7] = Text()
    tool_call_id: DreamingMutation.Col[str] = Text()
    actor: DreamingMutation.Col[DreamingMutationActor] = Text()
    operation: DreamingMutation.Col[DreamingMutationOperation] = Text()
    workspace_path: DreamingMutation.Col[str] = Text()
    payload: DreamingMutation.Col[str | None] = Text(default=None, nullable=True)
    status: DreamingMutation.Col[DreamingMutationStatus] = Text(
        default=cast("DreamingMutationStatus", "executed")
    )
    attempts: DreamingMutation.Col[int] = Integer(default=1)
    error: DreamingMutation.Col[str | None] = Text(default=None, nullable=True)
    created_at: DreamingMutation.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: DreamingMutation.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(run_id, tool_call_id)]


class DreamingWorkspaceFile[S = Pending](Model[S, "DreamingWorkspaceFile[Fetched]"]):
    """Canonical current row for one workspace path and its latest bytes."""

    path: DreamingWorkspaceFile.GenCol[str] = Text(primary_key=True)
    content_hash: DreamingWorkspaceFile.Col[str] = Text()
    content: DreamingWorkspaceFile.Col[str | None] = Text(nullable=True)
    is_tombstone: DreamingWorkspaceFile.Col[int] = Integer(default=0)
    version: DreamingWorkspaceFile.Col[PositiveInt] = Integer(default=1)
    source_run_id: DreamingWorkspaceFile.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    source_tool_call_id: DreamingWorkspaceFile.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    actor: DreamingWorkspaceFile.Col[DreamingMutationActor | None] = Text(
        default=None,
        nullable=True,
    )
    created_at: DreamingWorkspaceFile.GenCol[UtcDatetime] = Text(
        default=CurrentTimestamp
    )
    updated_at: DreamingWorkspaceFile.GenCol[UtcDatetime] = Text(
        default=CurrentTimestamp
    )


class DreamConversationCursor[S = Pending](
    Model[S, "DreamConversationCursor[Fetched]"]
):
    """Per-conversation high-water mark for assimilated transcript messages."""

    conversation_id: DreamConversationCursor.GenCol[UUID7] = Text(primary_key=True)
    last_assimilated_seq: DreamConversationCursor.Col[int] = Integer(default=0)
    updated_at: DreamConversationCursor.GenCol[UtcDatetime] = Text(
        default=CurrentTimestamp
    )


class DreamRun[S = Pending](Model[S, "DreamRun[Fetched]"]):
    """One requested Dreaming consolidation run against one conversation."""

    id: DreamRun.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    conversation_id: DreamRun.Col[UUID7] = Text()
    kind: DreamRun.Col[DreamRunKind] = Text()
    status: DreamRun.Col[DreamRunStatus] = Text()
    evidence_start_seq: DreamRun.Col[PositiveInt] = Integer()
    evidence_end_seq: DreamRun.Col[PositiveInt] = Integer()
    attempts: DreamRun.Col[PositiveInt] = Integer(default=1)
    error: DreamRun.Col[str | None] = Text(default=None, nullable=True)
    completed_at: DreamRun.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    created_at: DreamRun.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: DreamRun.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


_DREAM_MIGRATIONS = {
    f"{prefix:03d}_{label}": sql
    for prefix, (label, sql) in enumerate(
        scaffold_sqlite_statements(
            (DreamConversationCursor, DreamRun, DreamingMutation, DreamingWorkspaceFile)
        ),
        start=1,
    )
}
"""Ordered startup migrations for Dreaming state tables."""


async def create_dreaming_schema(database: Database) -> None:
    """Create Dreaming persistence tables on an initialized database."""
    await database.migrate(_DREAM_MIGRATIONS)


__all__ = [
    "DreamConversationCursor",
    "DreamRun",
    "DreamRunKind",
    "DreamRunStatus",
    "DreamRunTerminalStatus",
    "DreamingMutation",
    "DreamingMutationActor",
    "DreamingMutationOperation",
    "DreamingMutationStatus",
    "DreamingWorkspaceFile",
    "create_dreaming_schema",
]
