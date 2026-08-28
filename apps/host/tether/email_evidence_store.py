"""Read-only persistence compatibility for retained email snapshots."""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid7

from pydantic import UUID7
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


class EmailEvidenceSnapshot[S = Pending](Model[S, "EmailEvidenceSnapshot[Fetched]"]):
    """One immutable, explicitly promoted remote email source."""

    id: EmailEvidenceSnapshot.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    body_chars: EmailEvidenceSnapshot.Col[int] = Integer()
    body_text: EmailEvidenceSnapshot.Col[str] = Text()
    body_truncated: EmailEvidenceSnapshot.Col[int] = Integer()
    content_hash: EmailEvidenceSnapshot.Col[str] = Text()
    date_header: EmailEvidenceSnapshot.Col[str] = Text(default="")
    from_header: EmailEvidenceSnapshot.Col[str] = Text(default="")
    gmail_message_id: EmailEvidenceSnapshot.Col[str] = Text()
    subject: EmailEvidenceSnapshot.Col[str] = Text(default="")
    thread_id: EmailEvidenceSnapshot.Col[str] = Text()
    captured_at: EmailEvidenceSnapshot.GenCol[UtcDatetime] = Text(
        default=CurrentTimestamp
    )

    __indexes__: ClassVar = [Index(gmail_message_id, content_hash, unique=True)]


_EMAIL_EVIDENCE_MIGRATIONS = {
    "001_create_email_evidence_snapshot": (
        'CREATE TABLE "email_evidence_snapshot" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "body_chars" INTEGER NOT NULL, '
        '"body_text" TEXT NOT NULL, "body_truncated" INTEGER NOT NULL, '
        '"content_hash" TEXT NOT NULL, "date_header" TEXT NOT NULL, '
        '"from_header" TEXT NOT NULL, "gmail_message_id" TEXT NOT NULL, '
        '"subject" TEXT NOT NULL, "thread_id" TEXT NOT NULL, '
        '"captured_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "002_email_evidence_snapshot_source_unique": (
        'CREATE UNIQUE INDEX "email_evidence_snapshot_source_unique" '
        'ON "email_evidence_snapshot" ("gmail_message_id", "content_hash")'
    ),
    "003_create_email_evidence_promotion": (
        'CREATE TABLE "email_evidence_promotion" ('
        '"id" TEXT PRIMARY KEY NOT NULL, '
        '"authorizing_conversation_id" TEXT NOT NULL, '
        '"authorizing_message_id" TEXT NOT NULL, '
        '"authorizing_message_seq" INTEGER NOT NULL, '
        '"claim_hint" TEXT NOT NULL, "snapshot_id" TEXT NOT NULL, '
        '"created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
    ),
    "004_email_evidence_promotion_source_unique": (
        'CREATE UNIQUE INDEX "email_evidence_promotion_source_unique" '
        'ON "email_evidence_promotion" '
        '("snapshot_id", "authorizing_message_id", "claim_hint")'
    ),
}


async def create_email_evidence_schema(database: Database) -> None:
    """Preserve the historical email Evidence schema without new writes."""
    await database.migrate(_EMAIL_EVIDENCE_MIGRATIONS)


__all__ = [
    "EmailEvidenceSnapshot",
    "create_email_evidence_schema",
]
