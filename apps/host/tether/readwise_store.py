"""Persisted Readwise highlight mappings and synchronization cursors."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Json
from snekql import sqlite
from snekql.sqlite import (
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)

HIGHLIGHTS_WATERMARK_KEY = "highlights_export_watermark"
"""Cursor for the last successful Readwise Export pass."""
READER_WATERMARK_KEY = "reader_list_watermark"
"""Cursor for the last successful Reader document pass."""

type ReadwiseSyncKey = Literal["highlights_export_watermark", "reader_list_watermark"]
"""Known synchronization cursor identities."""


class ReadwiseHighlight[S = Pending](Model[S, "ReadwiseHighlight[Fetched]"]):
    """Canonical Readwise highlight Evidence retained for Dreaming."""

    highlight_id: sqlite.Col[int] = Integer(primary_key=True)
    content: sqlite.Col[str] = Text(nullable=False)
    metadata: sqlite.Col[Json[dict[str, str]]] = Text(default_factory=dict[str, str])
    updated_at: sqlite.Col[str] = Text(nullable=False)


class ReadwiseSyncState[S = Pending](Model[S, "ReadwiseSyncState[Fetched]"]):
    """Durable key/value synchronization state."""

    key: sqlite.Col[str] = Text(primary_key=True)
    value: sqlite.Col[str] = Text(nullable=False)


def _parse_datetime(raw: str) -> datetime | None:
    """Parse an ISO timestamp while tolerating malformed historical state."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def read_sync_watermark(
    database: Database, key: ReadwiseSyncKey
) -> datetime | None:
    """Read one successful-pass cursor, or `None` before the first pass."""
    async with database.transaction() as transaction:
        row = await transaction.fetch_one_or_none(
            select(ReadwiseSyncState).where(ReadwiseSyncState.key.eq(key))
        )
    return _parse_datetime(row.value) if row is not None else None


async def write_sync_watermark(
    database: Database, key: ReadwiseSyncKey, watermark: datetime
) -> None:
    """Upsert one successful-pass cursor."""

    async def _write(transaction: Transaction) -> None:
        existing = await transaction.fetch_one_or_none(
            select(ReadwiseSyncState).where(ReadwiseSyncState.key.eq(key))
        )
        if existing is None:
            _ = await transaction.execute(
                insert(ReadwiseSyncState(key=key, value=watermark.isoformat()))
            )
            return
        _ = await transaction.execute(
            update(ReadwiseSyncState)
            .set(ReadwiseSyncState.value.to(watermark.isoformat()))
            .where(ReadwiseSyncState.key.eq(key))
        )

    async with database.transaction(mode="immediate") as transaction:
        await _write(transaction)


_READWISE_MIGRATIONS: dict[str, str] = {
    # Highlight-to-Memory idempotency mapping, keyed by Readwise's stable
    # integer highlight id. Frozen at authoring time.
    "001_create_readwise_highlight": (
        'CREATE TABLE "readwise_highlight" ('
        '"highlight_id" INTEGER PRIMARY KEY NOT NULL, '
        '"memory_id" TEXT NOT NULL, '
        '"updated_at" TEXT NOT NULL'
        ") STRICT"
    ),
    # Sync-state key/value store (the export watermark). Frozen.
    "002_create_readwise_sync_state": (
        'CREATE TABLE "readwise_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    ),
    # The #507 cutover keeps highlights as source-owned Evidence instead of
    # mappings to removed loose/tethered Memory rows. Existing mappings contain
    # no source bytes and are intentionally discarded.
    "019_drop_readwise_memory_mapping": 'DROP TABLE "readwise_highlight"',
    "020_create_readwise_evidence": (
        'CREATE TABLE "readwise_highlight" ('
        '"highlight_id" INTEGER PRIMARY KEY NOT NULL, '
        '"content" TEXT NOT NULL, '
        "\"metadata\" TEXT NOT NULL DEFAULT '{}', "
        '"updated_at" TEXT NOT NULL'
        ") STRICT"
    ),
    # Existing mappings had no source bytes. Force one full export so the new
    # Evidence table is repopulated instead of resuming past every old highlight.
    "029_reset_readwise_highlight_watermark": (
        'DELETE FROM "readwise_sync_state" '
        "WHERE \"key\" = 'highlights_export_watermark'"
    ),
}


async def create_readwise_schema(database: Database) -> None:
    """Apply the frozen Readwise persistence migration chain."""
    await database.migrate(_READWISE_MIGRATIONS)


__all__ = [
    "HIGHLIGHTS_WATERMARK_KEY",
    "READER_WATERMARK_KEY",
    "ReadwiseHighlight",
    "ReadwiseSyncKey",
    "ReadwiseSyncState",
    "create_readwise_schema",
    "read_sync_watermark",
    "write_sync_watermark",
]
