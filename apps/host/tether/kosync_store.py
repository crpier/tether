"""SQLite ownership for KOReader progress events and ebook identities."""

from __future__ import annotations

from typing import ClassVar

from snekql.sqlite import (
    PENDING_GENERATION,
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
    insert,
    select,
    update,
)

from tether.kosync_model import ProgressUpdate


class EbookProgressEvent[S = Pending](Model[S, "EbookProgressEvent[Fetched]"]):
    """One immutable reading-progress push received from a device."""

    id: EbookProgressEvent.GenCol[int] = Integer(
        primary_key=True, default=PENDING_GENERATION
    )
    document_hash: EbookProgressEvent.Col[str] = Text(nullable=False)
    percentage: EbookProgressEvent.Col[float] = Real(nullable=False)
    progress: EbookProgressEvent.Col[str] = Text(nullable=False)
    device: EbookProgressEvent.Col[str] = Text(nullable=False)
    device_id: EbookProgressEvent.Col[str] = Text(nullable=False)
    timestamp: EbookProgressEvent.Col[int] = Integer(nullable=False)
    received_at: EbookProgressEvent.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    __indexes__: ClassVar = [Index(document_hash)]


class EbookDocument[S = Pending](Model[S, "EbookDocument[Fetched]"]):
    """A reading-source document identity and completion state."""

    document_hash: EbookDocument.Col[str] = Text(primary_key=True)
    title: EbookDocument.Col[str | None] = Text(default=None, nullable=True)
    finished_at: EbookDocument.Col[str | None] = Text(default=None, nullable=True)
    created_at: EbookDocument.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: EbookDocument.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class KosyncStore:
    """Persist KOReader progress while keeping each write boundary explicit."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def touch_document(self, document_hash: str) -> EbookDocument[Fetched]:
        """Create an unseen document or touch it, returning its prior guard state."""
        async with self.database.transaction(mode="immediate") as transaction:
            existing = await transaction.fetch_one_or_none(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )
            if existing is None:
                return await transaction.execute(
                    insert(EbookDocument(document_hash=document_hash)).returning()
                )
            _ = await transaction.execute(
                update(EbookDocument)
                .set(EbookDocument.updated_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(document_hash))
            )
            return existing

    async def append_event(
        self, progress_update: ProgressUpdate, server_timestamp: int
    ) -> None:
        """Append one immutable progress event with the server receipt time."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                insert(
                    EbookProgressEvent(
                        document_hash=progress_update.document,
                        percentage=progress_update.percentage,
                        progress=progress_update.progress,
                        device=progress_update.device,
                        device_id=progress_update.device_id,
                        timestamp=server_timestamp,
                    )
                )
            )

    async def fetch_latest_event(
        self, document_hash: str
    ) -> EbookProgressEvent[Fetched] | None:
        """Fetch the newest event for a document, if one has been received."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(EbookProgressEvent)
                .where(EbookProgressEvent.document_hash.eq(document_hash))
                .order_by(EbookProgressEvent.id.desc())
                .limit(1)
            )

    async def label_document(
        self, document_hash: str, title: str
    ) -> EbookDocument[Fetched]:
        """Attach a title, creating the document identity when unseen."""
        async with self.database.transaction(mode="immediate") as transaction:
            existing = await transaction.fetch_one_or_none(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )
            if existing is None:
                return await transaction.execute(
                    insert(
                        EbookDocument(document_hash=document_hash, title=title)
                    ).returning()
                )
            _ = await transaction.execute(
                update(EbookDocument)
                .set(
                    EbookDocument.title.to(title),
                    EbookDocument.updated_at.to(CurrentTimestamp),
                )
                .where(EbookDocument.document_hash.eq(document_hash))
            )
            return await transaction.fetch_one(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )

    async def list_unlabeled(self) -> list[EbookDocument[Fetched]]:
        """List documents without titles from oldest to newest."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(EbookDocument)
                .where(EbookDocument.title.is_null())
                .order_by(EbookDocument.created_at.asc())
            )

    async def stamp_finished(self, document_hash: str) -> None:
        """Record the source document's first observed completion."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(EbookDocument)
                .set(EbookDocument.finished_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(document_hash))
            )


_KOSYNC_MIGRATIONS: dict[str, str] = {
    # Append-only progress Telemetry, one row per device push. Frozen at
    # authoring time; the model above must keep matching this shape.
    "001_create_ebook_progress_event": (
        'CREATE TABLE "ebook_progress_event" ('
        '"id" INTEGER PRIMARY KEY, '
        '"document_hash" TEXT NOT NULL, '
        '"percentage" REAL NOT NULL, '
        '"progress" TEXT NOT NULL, '
        '"device" TEXT NOT NULL, '
        '"device_id" TEXT NOT NULL, '
        '"timestamp" INTEGER NOT NULL, '
        "\"received_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    # Lookup index for the furthest-progress query and per-document reads.
    "002_index_ebook_progress_event_document_hash": (
        'CREATE INDEX "ix_ebook_progress_event_document_hash" '
        'ON "ebook_progress_event" ("document_hash")'
    ),
    # Per-document hash→title mapping plus the finished-once guard. Frozen.
    "003_create_ebook_document": (
        'CREATE TABLE "ebook_document" ('
        '"document_hash" TEXT PRIMARY KEY NOT NULL, '
        '"title" TEXT, '
        '"finished_captured_at" TEXT, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    "028_rename_ebook_finished_at": (
        'ALTER TABLE "ebook_document" RENAME COLUMN '
        '"finished_captured_at" TO "finished_at"'
    ),
}


async def create_kosync_schema(database: Database) -> None:
    """Bring the kosync Telemetry schema to current on an initialized database.

    Applies the frozen migration chain: the append-only progress-event table,
    its lookup index, and the per-document mapping table. The caller owns
    `Database.initialize` and hands the live database here before serving.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_kosync_schema(database)
    """
    await database.migrate(_KOSYNC_MIGRATIONS)
