"""SQLite ownership for mirrored KOReader books, page events, and watermark."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from snekql import sqlite
from snekql.sqlite import (
    PENDING_GENERATION,
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    select,
    update,
)

from tether.ebook_stats_model import ParsedBook, ParsedPageEvent
from tether.kosync_store import EbookDocument

_WATERMARK_KEY = "statistics_file_watermark"
"""Key for the last fully ingested source file's `mtime_ns:size`."""


class EbookStatBook[S = Pending](Model[S, "EbookStatBook[Fetched]"]):
    """One upstream KOReader `book` row mirrored into canonical SQLite."""

    id: sqlite.GenCol[int] = Integer(primary_key=True, default=PENDING_GENERATION)
    source_book_id: sqlite.Col[int] = Integer(nullable=False)
    title: sqlite.Col[str | None] = Text(default=None, nullable=True)
    authors: sqlite.Col[str | None] = Text(default=None, nullable=True)
    pages: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    md5: sqlite.Col[str | None] = Text(default=None, nullable=True)
    total_read_time: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    total_read_pages: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    highlights: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    notes: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    last_open: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    document_hash: sqlite.Col[str | None] = Text(default=None, nullable=True)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    __indexes__: ClassVar = [Index(source_book_id, unique=True)]


class EbookStatPageEvent[S = Pending](Model[S, "EbookStatPageEvent[Fetched]"]):
    """One append-only per-page event, unique by book, page, and start time."""

    id: sqlite.GenCol[int] = Integer(primary_key=True, default=PENDING_GENERATION)
    book: sqlite.Col[int] = Integer(nullable=False)
    page: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    duration: sqlite.Col[int] = Integer(nullable=False)
    __indexes__: ClassVar = [
        Index(book, start_time),
        Index(book, page, start_time, unique=True),
    ]


class EbookStatSyncState[S = Pending](Model[S, "EbookStatSyncState[Fetched]"]):
    """Durable key/value state for successful file ingestion."""

    key: sqlite.Col[str] = Text(primary_key=True)
    value: sqlite.Col[str] = Text(nullable=False)


def _group_events_by_book(
    events: tuple[ParsedPageEvent, ...],
) -> dict[int, list[ParsedPageEvent]]:
    """Group page events by their foreign source-book identity."""
    grouped: dict[int, list[ParsedPageEvent]] = {}
    for event in events:
        grouped.setdefault(event.source_book_id, []).append(event)
    return grouped


class EbookStatsStore:
    """Persist parsed statistics and advance state only when explicitly asked."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def upsert_books(self, books: tuple[ParsedBook, ...]) -> dict[int, int]:
        """Upsert parsed books and map each foreign id to its canonical row id."""
        book_id_by_source_id: dict[int, int] = {}
        for parsed_book in books:
            row = await self._upsert_book(parsed_book)
            book_id_by_source_id[parsed_book.source_book_id] = row.id
        return book_id_by_source_id

    async def insert_events(
        self,
        events: tuple[ParsedPageEvent, ...],
        book_id_by_source_id: dict[int, int],
    ) -> int:
        """Insert page events whose books exist, skipping natural-key duplicates."""
        inserted = 0
        for source_book_id, book_events in _group_events_by_book(events).items():
            book_id = book_id_by_source_id.get(source_book_id)
            if book_id is None:
                continue
            inserted += await self._insert_book_events(book_id, book_events)
        return inserted

    async def read_watermark(self) -> str | None:
        """Read the watermark from the last fully successful pass."""
        async with self.database.transaction() as transaction:
            row = await transaction.fetch_one_or_none(
                select(EbookStatSyncState).where(
                    EbookStatSyncState.key.eq(_WATERMARK_KEY)
                )
            )
        return row.value if row is not None else None

    async def write_watermark(self, watermark: str) -> None:
        """Upsert the watermark after all books and events have persisted."""
        async with self.database.transaction(mode="immediate") as transaction:
            existing = await transaction.fetch_one_or_none(
                select(EbookStatSyncState).where(
                    EbookStatSyncState.key.eq(_WATERMARK_KEY)
                )
            )
            if existing is None:
                _ = await transaction.execute(
                    insert(EbookStatSyncState(key=_WATERMARK_KEY, value=watermark))
                )
                return
            _ = await transaction.execute(
                update(EbookStatSyncState)
                .set(EbookStatSyncState.value.to(watermark))
                .where(EbookStatSyncState.key.eq(_WATERMARK_KEY))
            )

    async def _upsert_book(self, parsed_book: ParsedBook) -> EbookStatBook[Fetched]:
        """Upsert one book while retaining a prior best-effort document link."""
        async with self.database.transaction(mode="immediate") as transaction:
            existing = await transaction.fetch_one_or_none(
                select(EbookStatBook).where(
                    EbookStatBook.source_book_id.eq(parsed_book.source_book_id)
                )
            )
            document_hash = existing.document_hash if existing is not None else None
            if parsed_book.title:
                matched_document = await transaction.fetch_one_or_none(
                    select(EbookDocument).where(
                        EbookDocument.title.eq(parsed_book.title)
                    )
                )
                if matched_document is not None:
                    document_hash = matched_document.document_hash
            if existing is None:
                return await transaction.execute(
                    insert(
                        EbookStatBook(
                            source_book_id=parsed_book.source_book_id,
                            title=parsed_book.title,
                            authors=parsed_book.authors,
                            pages=parsed_book.pages,
                            md5=parsed_book.md5,
                            total_read_time=parsed_book.total_read_time,
                            total_read_pages=parsed_book.total_read_pages,
                            highlights=parsed_book.highlights,
                            notes=parsed_book.notes,
                            last_open=parsed_book.last_open,
                            document_hash=document_hash,
                        )
                    ).returning()
                )
            _ = await transaction.execute(
                update(EbookStatBook)
                .set(
                    EbookStatBook.title.to(parsed_book.title),
                    EbookStatBook.authors.to(parsed_book.authors),
                    EbookStatBook.pages.to(parsed_book.pages),
                    EbookStatBook.md5.to(parsed_book.md5),
                    EbookStatBook.total_read_time.to(parsed_book.total_read_time),
                    EbookStatBook.total_read_pages.to(parsed_book.total_read_pages),
                    EbookStatBook.highlights.to(parsed_book.highlights),
                    EbookStatBook.notes.to(parsed_book.notes),
                    EbookStatBook.last_open.to(parsed_book.last_open),
                    EbookStatBook.document_hash.to(document_hash),
                    EbookStatBook.updated_at.to(CurrentTimestamp),
                )
                .where(EbookStatBook.source_book_id.eq(parsed_book.source_book_id))
            )
            return await transaction.fetch_one(
                select(EbookStatBook).where(
                    EbookStatBook.source_book_id.eq(parsed_book.source_book_id)
                )
            )

    async def _insert_book_events(
        self, book_id: int, events: Iterable[ParsedPageEvent]
    ) -> int:
        """Insert one book's page events without duplicating natural keys."""
        existing_keys = await self._existing_event_keys(book_id)
        async with self.database.transaction(mode="immediate") as transaction:
            inserted = 0
            for event in events:
                key = (event.page, event.start_time)
                if key in existing_keys:
                    continue
                _ = await transaction.execute(
                    insert(
                        EbookStatPageEvent(
                            book=book_id,
                            page=event.page,
                            start_time=event.start_time,
                            duration=event.duration,
                        )
                    )
                )
                existing_keys.add(key)
                inserted += 1
            return inserted

    async def _existing_event_keys(self, book_id: int) -> set[tuple[int, int]]:
        """Read page/start-time natural keys already stored for a book."""
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(
                select(EbookStatPageEvent).where(EbookStatPageEvent.book.eq(book_id))
            )
        return {(row.page, row.start_time) for row in rows}


_EBOOK_STATS_MIGRATIONS: dict[str, str] = {
    # One row per upstream `book`, keyed by our own id. Frozen at authoring
    # time; the model above must keep matching this shape.
    "001_create_ebook_stat_book": (
        'CREATE TABLE "ebook_stat_book" ('
        '"id" INTEGER PRIMARY KEY, '
        '"source_book_id" INTEGER NOT NULL, '
        '"title" TEXT, '
        '"authors" TEXT, '
        '"pages" INTEGER, '
        '"md5" TEXT, '
        '"total_read_time" INTEGER, '
        '"total_read_pages" INTEGER, '
        '"highlights" INTEGER, '
        '"notes" INTEGER, '
        '"last_open" INTEGER, '
        '"document_hash" TEXT, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    # Uniqueness on the upstream book id, so re-parsing upserts the same row.
    "002_index_ebook_stat_book_source_book_id": (
        'CREATE UNIQUE INDEX "ux_ebook_stat_book_source_book_id" '
        'ON "ebook_stat_book" ("source_book_id")'
    ),
    # Append-only per-page read event. Frozen.
    "003_create_ebook_stat_page_event": (
        'CREATE TABLE "ebook_stat_page_event" ('
        '"id" INTEGER PRIMARY KEY, '
        '"book" INTEGER NOT NULL, '
        '"page" INTEGER NOT NULL, '
        '"start_time" INTEGER NOT NULL, '
        '"duration" INTEGER NOT NULL'
        ") STRICT"
    ),
    # Lookup index for per-book event reads.
    "004_index_ebook_stat_page_event_book_start_time": (
        'CREATE INDEX "ix_ebook_stat_page_event_book_start_time" '
        'ON "ebook_stat_page_event" ("book", "start_time")'
    ),
    # The idempotency key: re-ingesting the same or an overlapping snapshot
    # never duplicates a row.
    "005_index_ebook_stat_page_event_natural_key": (
        'CREATE UNIQUE INDEX "ux_ebook_stat_page_event_book_page_start_time" '
        'ON "ebook_stat_page_event" ("book", "page", "start_time")'
    ),
    # Sync-state key/value store (the file watermark). Frozen.
    "006_create_ebook_stat_sync_state": (
        'CREATE TABLE "ebook_stat_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    ),
}


async def create_ebook_stats_schema(database: Database) -> None:
    """Bring the ebook stats Telemetry schema to current on an initialized database.

    Applies the frozen migration chain: the book table and its uniqueness
    index, the page-event table with its lookup and idempotency indexes, and
    the sync-state key/value store. The caller owns `Database.initialize` and
    hands the live database here before serving.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_ebook_stats_schema(database)
    """
    await database.migrate(_EBOOK_STATS_MIGRATIONS)
