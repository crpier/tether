"""Persistence model for immutable files attached to Conversation Messages."""

from __future__ import annotations

from typing import Literal
from uuid import uuid7

from pydantic import UUID7, NonNegativeInt, PositiveInt
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)

type AttachmentKind = Literal["document", "image"]


class MessageAttachment[S = Pending](Model[S, "MessageAttachment[Fetched]"]):
    """One immutable uploaded file and its Conversation lifecycle links."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    conversation_id: sqlite.Col[UUID7] = Text()
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    extracted_text: sqlite.Col[str | None] = Text(default=None, nullable=True)
    extraction_truncated: sqlite.Col[bool] = Integer(default=False)
    filename: sqlite.Col[str] = Text()
    kind: sqlite.Col[AttachmentKind] = Text()
    message_id: sqlite.Col[UUID7 | None] = Text(default=None, nullable=True)
    mime_type: sqlite.Col[str] = Text()
    size_bytes: sqlite.Col[NonNegativeInt] = Integer()
    turn_id: sqlite.Col[UUID7 | None] = Text(default=None, nullable=True)
    turn_position: sqlite.Col[PositiveInt | None] = Integer(default=None, nullable=True)


async def create_attachment_schema(database: Database) -> None:
    """Create immutable attachment metadata and lifecycle indexes."""
    await database.migrate(
        {
            "038_create_message_attachment": (
                'CREATE TABLE "message_attachment" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"conversation_id" TEXT NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"extracted_text" TEXT, '
                '"extraction_truncated" INTEGER NOT NULL DEFAULT 0, '
                '"filename" TEXT NOT NULL, '
                '"kind" TEXT NOT NULL, '
                '"message_id" TEXT, '
                '"mime_type" TEXT NOT NULL, '
                '"size_bytes" INTEGER NOT NULL, '
                '"turn_id" TEXT, '
                '"turn_position" INTEGER) STRICT'
            ),
            "038_attachment_message_index": (
                'CREATE INDEX "message_attachment_message_index" '
                'ON "message_attachment" ("message_id") '
                'WHERE "message_id" IS NOT NULL'
            ),
            "038_attachment_turn_index": (
                'CREATE UNIQUE INDEX "message_attachment_turn_index" '
                'ON "message_attachment" ("turn_id", "turn_position") '
                'WHERE "turn_id" IS NOT NULL'
            ),
        }
    )


__all__ = [
    "AttachmentKind",
    "MessageAttachment",
    "create_attachment_schema",
]
