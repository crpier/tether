"""Persisted Gmail ingestion models, cursors, and schema."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Model,
    Pending,
    Text,
    Transaction,
    UtcDatetime,
    insert,
    select,
    update,
)

GMAIL_WATERMARK_KEY = "gmail_message_watermark"
"""Cursor for the last fully successful Gmail ingestion pass."""

"""Independent cursor for the last successful inbox hygiene sweep."""

type GmailMessageStatus = Literal["prefiltered", "noise", "ingested", "pending"]
"""Resting state of one reviewed Gmail message."""


class GmailMessageRecord[S = Pending](Model[S, "GmailMessageRecord[Fetched]"]):
    """Idempotency and audit state for one Gmail message."""

    message_id: sqlite.Col[str] = Text(primary_key=True)
    status: sqlite.Col[GmailMessageStatus] = Text()
    trigger_id: sqlite.Col[str | None] = Text(default=None, nullable=True)
    internal_date: sqlite.Col[str] = Text()
    from_header: sqlite.Col[str] = Text(default="")
    subject: sqlite.Col[str] = Text(default="")
    body_text: sqlite.Col[str] = Text(default="")
    verdict_reason: sqlite.Col[str] = Text(default="")
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class GmailSyncState[S = Pending](Model[S, "GmailSyncState[Fetched]"]):
    """Durable key/value synchronization state."""

    key: sqlite.Col[str] = Text(primary_key=True)
    value: sqlite.Col[str] = Text(nullable=False)


def _parse_datetime(raw: str) -> datetime:
    """Read an aware cursor while accepting historical naive values as UTC."""
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def read_sync_watermark(database: Database, key: str) -> datetime | None:
    """Read one synchronization cursor by its stable state key."""
    async with database.transaction() as transaction:
        stored = await transaction.fetch_one_or_none(
            select(GmailSyncState).where(GmailSyncState.key.eq(key))
        )
    return _parse_datetime(stored.value) if stored is not None else None


async def write_sync_watermark(
    database: Database, key: str, watermark: datetime
) -> None:
    """Insert or replace one synchronization cursor."""

    async def _write(transaction: Transaction) -> None:
        stored = await transaction.fetch_one_or_none(
            select(GmailSyncState).where(GmailSyncState.key.eq(key))
        )
        if stored is None:
            _ = await transaction.execute(
                insert(GmailSyncState(key=key, value=watermark.isoformat()))
            )
            return
        _ = await transaction.execute(
            update(GmailSyncState)
            .set(GmailSyncState.value.to(watermark.isoformat()))
            .where(GmailSyncState.key.eq(key))
        )

    async with database.transaction(mode="immediate") as transaction:
        await _write(transaction)


_GMAIL_MIGRATIONS: dict[str, str] = {
    # Idempotency + audit table, keyed by Gmail's stable string message id.
    "001_create_gmail_message": (
        'CREATE TABLE "gmail_message_record" ('
        '"message_id" TEXT PRIMARY KEY NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"trigger_id" TEXT, '
        '"internal_date" TEXT NOT NULL, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    # Sync-state key/value store (the pass watermark).
    "002_create_gmail_sync_state": (
        'CREATE TABLE "gmail_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    ),
    # Preserve source bytes and triage context as ingestion audit state.
    # Explicit promotion owns the separate path into citeable Evidence.
    "021_gmail_from_header": (
        'ALTER TABLE "gmail_message_record" ADD COLUMN "from_header" '
        "TEXT NOT NULL DEFAULT ''"
    ),
    "022_gmail_subject": (
        'ALTER TABLE "gmail_message_record" ADD COLUMN "subject" '
        "TEXT NOT NULL DEFAULT ''"
    ),
    "023_gmail_body_text": (
        'ALTER TABLE "gmail_message_record" ADD COLUMN "body_text" '
        "TEXT NOT NULL DEFAULT ''"
    ),
    "024_gmail_verdict_reason": (
        'ALTER TABLE "gmail_message_record" ADD COLUMN "verdict_reason" '
        "TEXT NOT NULL DEFAULT ''"
    ),
    "025_gmail_drop_memory_id": (
        'UPDATE "gmail_message_record" SET "message_id" = "message_id" WHERE 0'
    ),
}


async def create_gmail_schema(database: Database) -> None:
    """Bring the Gmail ingestion schema to its current version."""
    await database.migrate(_GMAIL_MIGRATIONS)


__all__ = [
    "GMAIL_WATERMARK_KEY",
    "GmailMessageRecord",
    "GmailMessageStatus",
    "GmailSyncState",
    "create_gmail_schema",
    "read_sync_watermark",
    "write_sync_watermark",
]
