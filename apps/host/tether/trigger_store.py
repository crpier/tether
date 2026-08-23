"""Canonical persistence for Scheduled triggers and scheduler state."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID, uuid7

from pydantic import UUID7, NonNegativeInt, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Predicate,
    Text,
    UtcDatetime,
)

from tether.trigger_schedule import TriggerActionKind, TriggerRecurrence

type TriggerStatus = Literal["active", "completed", "failed"]
"""A trigger's firing lifecycle; recurring triggers stay `active` forever."""
type OccurrenceStatus = Literal[
    "pending", "running", "succeeded", "failed", "cancelled"
]
"""One immutable firing's execution lifecycle."""
type PushDeliveryStatus = Literal[
    "not_applicable", "pending", "delivering", "delivered", "failed"
]
"""Web Push delivery state, independent from prompt execution."""


class ScheduledTrigger[S = Pending](Model[S, "ScheduledTrigger[Fetched]"]):
    """A persisted time-triggered action and its scheduler lifecycle state."""

    id: ScheduledTrigger.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    recurrence: ScheduledTrigger.Col[TriggerRecurrence] = Text()
    """How often the trigger fires: `once`, `daily`, or `weekly`."""
    action_kind: ScheduledTrigger.Col[TriggerActionKind] = Text()
    """`message` delivers `payload` verbatim; `prompt` runs it through pi."""
    payload: ScheduledTrigger.Col[str] = Text()
    """The fixed message text, or the agent prompt, depending on `action_kind`."""
    model_profile: ScheduledTrigger.Col[str | None] = Text(default=None, nullable=True)
    """Profile pinned to a recurring prompt; null for other trigger actions."""
    target_conversation_id: ScheduledTrigger.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    """Active Conversation targeted by a prompt; null for fixed messages."""
    timezone: ScheduledTrigger.Col[str] = Text()
    """IANA timezone the wall-clock recurrence is anchored to."""
    wall_time: ScheduledTrigger.Col[str | None] = Text(default=None, nullable=True)
    """`HH:MM` wall-clock fire time for recurring triggers; null for `once`."""
    weekday: ScheduledTrigger.Col[int | None] = Integer(default=None, nullable=True)
    """Weekday (Mon=0) for `weekly`; null otherwise."""
    next_fire_at: ScheduledTrigger.Col[UtcDatetime] = Text()
    """The next scheduled occurrence, as UTC."""
    status: ScheduledTrigger.Col[TriggerStatus] = Text()
    """Firing lifecycle; recurring triggers remain `active`."""
    claimed_at: ScheduledTrigger.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    """Stamped when a scheduler tick claims the row for dispatch."""
    attempts: ScheduledTrigger.Col[int] = Integer(default=0)
    """Failed dispatch attempts at the current occurrence."""
    next_attempt_at: ScheduledTrigger.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    """Retry-backoff time; when set it overrides `next_fire_at` for due-ness."""
    last_error: ScheduledTrigger.Col[str | None] = Text(default=None, nullable=True)
    """The most recent dispatch failure message, for diagnostics."""
    version: ScheduledTrigger.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: ScheduledTrigger.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: ScheduledTrigger.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    deleted_at: ScheduledTrigger.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(status, next_fire_at)]


class ScheduledOccurrence[S = Pending](Model[S, "ScheduledOccurrence[Fetched]"]):
    """One durable firing whose definition snapshot never changes."""

    id: ScheduledOccurrence.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    action_kind: ScheduledOccurrence.Col[TriggerActionKind] = Text()
    answer: ScheduledOccurrence.Col[str | None] = Text(default=None, nullable=True)
    answer_message_id: ScheduledOccurrence.Col[UUID | None] = Text(
        default=None,
        nullable=True,
    )
    completed_at: ScheduledOccurrence.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    created_at: ScheduledOccurrence.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    dispatch_attempts: ScheduledOccurrence.Col[NonNegativeInt] = Integer(default=0)
    failure_code: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    failure_summary: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    intended_fire_at: ScheduledOccurrence.Col[UtcDatetime] = Text()
    model_display_name_snapshot: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_id_snapshot: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_profile: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_provider_snapshot: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    model_thinking_level_snapshot: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    next_attempt_at: ScheduledOccurrence.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    payload: ScheduledOccurrence.Col[str] = Text()
    push_attempts: ScheduledOccurrence.Col[NonNegativeInt] = Integer(default=0)
    push_error: ScheduledOccurrence.Col[str | None] = Text(
        default=None,
        nullable=True,
    )
    push_next_attempt_at: ScheduledOccurrence.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    push_status: ScheduledOccurrence.Col[PushDeliveryStatus] = Text()
    pushed_at: ScheduledOccurrence.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    started_at: ScheduledOccurrence.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    status: ScheduledOccurrence.Col[OccurrenceStatus] = Text()
    target_conversation_id: ScheduledOccurrence.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    trigger_id: ScheduledOccurrence.Col[UUID7] = Text()
    trigger_version: ScheduledOccurrence.Col[PositiveInt] = Integer()

    __indexes__: ClassVar = [Index(trigger_id, intended_fire_at)]


def due_trigger_predicate(now: datetime) -> Predicate[ScheduledTrigger[Pending]]:
    """Select live, active, unclaimed triggers whose effective clock is due."""
    retry_due = ScheduledTrigger.next_attempt_at.is_not_null() & (
        ScheduledTrigger.next_attempt_at.lte(now)
    )
    fire_due = ScheduledTrigger.next_attempt_at.is_null() & (
        ScheduledTrigger.next_fire_at.lte(now)
    )
    return (
        ScheduledTrigger.deleted_at.is_null()
        & ScheduledTrigger.status.eq("active")
        & ScheduledTrigger.claimed_at.is_null()
        & (retry_due | fire_due)
    )


async def create_trigger_schema(database: Database) -> None:
    """Create or upgrade Scheduled trigger persistence."""
    migrations = {
        "005_create_scheduled_trigger": (
            'CREATE TABLE "scheduled_trigger" ("id" TEXT PRIMARY KEY NOT NULL, '
            '"recurrence" TEXT NOT NULL, "action_kind" TEXT NOT NULL, '
            '"payload" TEXT NOT NULL, "timezone" TEXT NOT NULL, '
            '"wall_time" TEXT, "weekday" INTEGER, "next_fire_at" TEXT NOT NULL, '
            '"status" TEXT NOT NULL, "claimed_at" TEXT, '
            '"attempts" INTEGER NOT NULL, "next_attempt_at" TEXT, '
            '"last_error" TEXT, "version" INTEGER NOT NULL, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"updated_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"deleted_at" TEXT) STRICT'
        ),
        "005_create_index_ix_scheduled_trigger_status_next_fire_at": (
            'CREATE INDEX "ix_scheduled_trigger_status_next_fire_at" '
            'ON "scheduled_trigger" ("status", "next_fire_at")'
        ),
        "033_scheduled_trigger_model_profile": (
            'ALTER TABLE "scheduled_trigger" ADD COLUMN "model_profile" TEXT'
        ),
        "036_scheduled_trigger_target_conversation": (
            'ALTER TABLE "scheduled_trigger" ADD COLUMN "target_conversation_id" TEXT'
        ),
        "036_create_scheduled_occurrence": (
            'CREATE TABLE "scheduled_occurrence" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "action_kind" TEXT NOT NULL, '
            '"answer" TEXT, "answer_message_id" TEXT, "completed_at" TEXT, '
            '"created_at" TEXT NOT NULL DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"failure_code" TEXT, "failure_summary" TEXT, '
            '"intended_fire_at" TEXT NOT NULL, "model_profile" TEXT, '
            '"payload" TEXT NOT NULL, "push_attempts" INTEGER NOT NULL '
            'DEFAULT 0, "push_error" TEXT, "push_next_attempt_at" TEXT, '
            '"push_status" TEXT NOT NULL, "pushed_at" TEXT, '
            '"started_at" TEXT, "status" TEXT NOT NULL, '
            '"target_conversation_id" TEXT, "trigger_id" TEXT NOT NULL, '
            '"trigger_version" INTEGER NOT NULL) STRICT'
        ),
        "036_scheduled_occurrence_trigger_fire_unique": (
            'CREATE UNIQUE INDEX "scheduled_occurrence_trigger_fire_unique" '
            'ON "scheduled_occurrence" ("trigger_id", "intended_fire_at")'
        ),
        "036_scheduled_occurrence_status_push_index": (
            'CREATE INDEX "scheduled_occurrence_status_push_index" '
            'ON "scheduled_occurrence" ("status", "push_status", '
            '"push_next_attempt_at")'
        ),
        "038_occurrence_dispatch_attempts": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN "dispatch_attempts" '
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "038_occurrence_model_display_name_snapshot": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN '
            '"model_display_name_snapshot" TEXT'
        ),
        "038_occurrence_model_id_snapshot": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN "model_id_snapshot" TEXT'
        ),
        "038_occurrence_model_provider_snapshot": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN '
            '"model_provider_snapshot" TEXT'
        ),
        "038_occurrence_model_thinking_level_snapshot": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN '
            '"model_thinking_level_snapshot" TEXT'
        ),
        "038_occurrence_next_attempt_at": (
            'ALTER TABLE "scheduled_occurrence" ADD COLUMN "next_attempt_at" TEXT'
        ),
        "038_occurrence_retry_index": (
            'CREATE INDEX "scheduled_occurrence_retry_index" ON '
            '"scheduled_occurrence" ("status", "next_attempt_at")'
        ),
    }
    await database.migrate(migrations)


__all__ = [
    "OccurrenceStatus",
    "PushDeliveryStatus",
    "ScheduledOccurrence",
    "ScheduledTrigger",
    "TriggerStatus",
    "create_trigger_schema",
    "due_trigger_predicate",
]
