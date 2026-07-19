"""KOReader `statistics.sqlite` ingestion: on-device reading stats as Telemetry.

KOReader's on-device stats plugin writes `statistics.sqlite` locally; Syncthing
(outside Tether) mirrors a copy to a host-visible path. This module periodically
stats that path and, when it changed, ingests it into new Telemetry tables in
the main tether database — a pull/file-based sibling of the kosync progress gate
(`tether.kosync`) and the Readwise Reader v3 rider (`tether.readwise`).

Three concerns:

- **Safe reading.** The live path may be mid-write (Syncthing), so the source
  file is never opened directly: it is copied to a private temp snapshot,
  opened read-only and immutable via a sqlite URI, parsed, and the snapshot is
  deleted (success or failure). Parsing uses stdlib `sqlite3`, never `snekql`
  — the foreign file is not our database.
- **Storage.** `EbookStatBook` mirrors one upstream `book` row (KOReader's
  `md5`, read totals, etc.), opportunistically linked to `EbookDocument`
  (`tether.kosync`) by title match only — the two hash schemes disagree, so
  this link is best-effort and never force-reconciled. `EbookStatPageEvent` is
  an append-only per-page read event, idempotent on `(book, page,
  start_time)` so re-parsing a snapshot never duplicates rows.
- **Cheap polling.** A watermark of the source file's mtime + size is
  persisted after every successful parse, so an interval tick that finds the
  file unchanged skips parsing entirely.

>>> service = EbookStatsSyncService(database=database, statistics_db_path=path)
>>> report = await service.sync(logger=logger)
>>> report.books_upserted
1
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from anyio import NamedTemporaryFile
from anyio import Path as AsyncPath
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
    Transaction,
    insert,
    select,
    update,
)

from tether.db_retry import run_in_transaction
from tether.kosync import EbookDocument
from tether.logging import Logger

_WATERMARK_KEY = "statistics_file_watermark"
"""Sync-state key under which the last fully-ingested file's `mtime_ns:size`
is persisted. Absence means no successful pass has run — the next tick always
parses."""

_BOOK_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "authors",
    "md5",
    "total_read_time",
    "total_read_pages",
    "highlights",
    "notes",
    "last_open",
    "pages",
)
"""Columns read from upstream `book`, KOReader's own schema. `id` is required;
every other column is read when present and left `None` when the upstream
schema is missing or has renamed it, so drift never hard-fails the parse."""

_PAGE_STAT_COLUMNS: tuple[str, ...] = ("id_book", "page", "start_time", "duration")
"""Columns read from upstream `page_stat_data`. All four are required — a
schema missing one of them yields no page events for that snapshot."""


class EbookStatBook[S = Pending](Model[S, "EbookStatBook[Fetched]"]):
    """One upstream KOReader `book` row mirrored from `statistics.sqlite`.

    Keyed by our own id; `source_book_id` is KOReader's local `book.id`
    (unique per source file), so re-parsing the same book upserts the same
    row. `md5` is KOReader's binary partial-hash of the file — distinct from
    kosync's filename-mode `document_hash` on `EbookDocument`. `document_hash`
    is a separate, nullable, opportunistic link to that table by title match
    only; the two hash schemes disagree, so the link is best-effort and never
    force-reconciled (a title that stops matching does not clear an existing
    link).
    """

    id: EbookStatBook.GenCol[int] = Integer(
        primary_key=True, default=PENDING_GENERATION
    )
    source_book_id: EbookStatBook.Col[int] = Integer(nullable=False)
    title: EbookStatBook.Col[str | None] = Text(default=None, nullable=True)
    authors: EbookStatBook.Col[str | None] = Text(default=None, nullable=True)
    pages: EbookStatBook.Col[int | None] = Integer(default=None, nullable=True)
    md5: EbookStatBook.Col[str | None] = Text(default=None, nullable=True)
    total_read_time: EbookStatBook.Col[int | None] = Integer(
        default=None, nullable=True
    )
    total_read_pages: EbookStatBook.Col[int | None] = Integer(
        default=None, nullable=True
    )
    highlights: EbookStatBook.Col[int | None] = Integer(default=None, nullable=True)
    notes: EbookStatBook.Col[int | None] = Integer(default=None, nullable=True)
    last_open: EbookStatBook.Col[int | None] = Integer(default=None, nullable=True)
    document_hash: EbookStatBook.Col[str | None] = Text(default=None, nullable=True)
    created_at: EbookStatBook.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: EbookStatBook.GenCol[datetime] = Text(default=CurrentTimestamp)
    __indexes__: ClassVar = [Index(source_book_id, unique=True)]


class EbookStatPageEvent[S = Pending](Model[S, "EbookStatPageEvent[Fetched]"]):
    """One append-only per-page read event mirrored from `page_stat_data`.

    `book` is the `EbookStatBook.id` foreign key (not KOReader's `book.id`).
    Idempotent on the natural key `(book, page, start_time)` — KOReader's own
    dedup key — enforced by a unique index, so re-ingesting the same or an
    overlapping snapshot never duplicates a row.
    """

    id: EbookStatPageEvent.GenCol[int] = Integer(
        primary_key=True, default=PENDING_GENERATION
    )
    book: EbookStatPageEvent.Col[int] = Integer(nullable=False)
    page: EbookStatPageEvent.Col[int] = Integer(nullable=False)
    start_time: EbookStatPageEvent.Col[int] = Integer(nullable=False)
    duration: EbookStatPageEvent.Col[int] = Integer(nullable=False)
    __indexes__: ClassVar = [
        Index(book, start_time),
        Index(book, page, start_time, unique=True),
    ]


class EbookStatSyncState[S = Pending](Model[S, "EbookStatSyncState[Fetched]"]):
    """Durable key/value sync state: the last-ingested file's mtime + size."""

    key: EbookStatSyncState.Col[str] = Text(primary_key=True)
    value: EbookStatSyncState.Col[str] = Text(nullable=False)


@dataclass(frozen=True, slots=True)
class ParsedBook:
    """One `book` row as read from a `statistics.sqlite` snapshot."""

    authors: str | None
    highlights: int | None
    last_open: int | None
    md5: str | None
    notes: int | None
    pages: int | None
    source_book_id: int
    title: str | None
    total_read_pages: int | None
    total_read_time: int | None


@dataclass(frozen=True, slots=True)
class ParsedPageEvent:
    """One `page_stat_data` row as read from a `statistics.sqlite` snapshot."""

    duration: int
    page: int
    source_book_id: int
    start_time: int


@dataclass(frozen=True, slots=True)
class ParsedStatistics:
    """The full parse of one `statistics.sqlite` snapshot."""

    books: tuple[ParsedBook, ...]
    page_events: tuple[ParsedPageEvent, ...]


@dataclass(frozen=True, slots=True)
class EbookStatsSyncReport:
    """The outcome of one ingestion pass.

    Exactly one of `skipped`/`failed` is true, or neither (a successful parse),
    in which case `books_upserted`/`events_inserted` describe what changed.
    """

    books_upserted: int = 0
    events_inserted: int = 0
    skipped: bool = False
    failed: bool = False


def _text_or_none(value: object) -> str | None:
    """A non-empty string as-is, else `None` — coerces a foreign row value."""
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    """An int/float row value coerced to `int`, else `None` (bools excluded)."""
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, int | float) else None


def _available_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """The column names a foreign table actually has, via `PRAGMA table_info`."""
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row["name"]) for row in rows}


def _parse_books(connection: sqlite3.Connection) -> tuple[ParsedBook, ...]:
    """Parse every `book` row present, tolerating schema drift in extra columns.

    Only columns from the known KOReader schema that are actually present get
    selected; a snapshot missing `id` yields no books at all rather than
    guessing a key.
    """
    available = _available_columns(connection, "book")
    if "id" not in available:
        return ()
    wanted = [column for column in _BOOK_COLUMNS if column in available]
    columns_sql = ", ".join(f'"{column}"' for column in wanted)
    rows = connection.execute(f"SELECT {columns_sql} FROM book").fetchall()  # noqa: S608
    books: list[ParsedBook] = []
    for row in rows:
        mapping = dict(row)
        books.append(
            ParsedBook(
                source_book_id=int(mapping["id"]),
                title=_text_or_none(mapping.get("title")),
                authors=_text_or_none(mapping.get("authors")),
                pages=_int_or_none(mapping.get("pages")),
                md5=_text_or_none(mapping.get("md5")),
                total_read_time=_int_or_none(mapping.get("total_read_time")),
                total_read_pages=_int_or_none(mapping.get("total_read_pages")),
                highlights=_int_or_none(mapping.get("highlights")),
                notes=_int_or_none(mapping.get("notes")),
                last_open=_int_or_none(mapping.get("last_open")),
            )
        )
    return tuple(books)


def _parse_page_events(connection: sqlite3.Connection) -> tuple[ParsedPageEvent, ...]:
    """Parse every `page_stat_data` row; empty when the schema lacks a needed column."""
    available = _available_columns(connection, "page_stat_data")
    if not set(_PAGE_STAT_COLUMNS).issubset(available):
        return ()
    columns_sql = ", ".join(f'"{column}"' for column in _PAGE_STAT_COLUMNS)
    rows = connection.execute(f"SELECT {columns_sql} FROM page_stat_data").fetchall()  # noqa: S608
    events: list[ParsedPageEvent] = []
    for row in rows:
        mapping = dict(row)
        book_id = _int_or_none(mapping.get("id_book"))
        page = _int_or_none(mapping.get("page"))
        start_time = _int_or_none(mapping.get("start_time"))
        duration = _int_or_none(mapping.get("duration"))
        if book_id is None or page is None or start_time is None or duration is None:
            continue
        events.append(
            ParsedPageEvent(
                source_book_id=book_id,
                page=page,
                start_time=start_time,
                duration=duration,
            )
        )
    return tuple(events)


def parse_statistics_file(path: Path) -> ParsedStatistics:
    """Parse a KOReader `statistics.sqlite` snapshot into books and page events.

    Opens the file read-only and immutable via a sqlite URI. The caller must
    hand this a private snapshot copy, never the live Syncthing-mirrored path
    — KOReader may have that file open. Runs synchronously (stdlib `sqlite3`
    has no async API); callers on the event loop run it in an executor.

    >>> statistics = parse_statistics_file(Path("/tmp/snapshot.sqlite"))  # doctest: +SKIP
    >>> statistics.books[0].source_book_id  # doctest: +SKIP
    1
    """
    uri = f"file:{path}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return ParsedStatistics(
            books=_parse_books(connection), page_events=_parse_page_events(connection)
        )
    finally:
        connection.close()


def _group_events_by_book(
    events: tuple[ParsedPageEvent, ...],
) -> dict[int, list[ParsedPageEvent]]:
    """Bucket page events by their upstream `source_book_id`."""
    grouped: dict[int, list[ParsedPageEvent]] = {}
    for event in events:
        grouped.setdefault(event.source_book_id, []).append(event)
    return grouped


class EbookStatsSyncService:
    """The statistics-file ingestion worker over the ebook stats Telemetry tables.

    An idempotent `sync` pass (run at boot and on a periodic loop) stats the
    configured file; an unchanged mtime/size is a cheap no-op. A changed file
    is snapshotted, parsed off-thread, and folded into `EbookStatBook` upserts
    (title-linked to `EbookDocument` when a match exists) and idempotent
    `EbookStatPageEvent` inserts. The watermark is persisted only after a fully
    successful parse, so a mid-pass failure re-parses on the next tick instead
    of silently skipping.
    """

    def __init__(self, database: Database, statistics_db_path: Path) -> None:
        self.database: Database = database
        self.statistics_db_path: Path = statistics_db_path

    async def sync(self, *, logger: Logger) -> EbookStatsSyncReport:
        """Run one pass; parse only when the source file changed since the watermark.

        A missing source file or a parse failure is logged and reported, never
        raised, so a caller looping this on an interval keeps retrying on the
        next tick rather than dying.
        """
        source = AsyncPath(self.statistics_db_path)
        try:
            file_stat = await source.stat()
        except OSError:
            logger.warning(
                "Ebook statistics file not found",
                path=str(self.statistics_db_path),
            )
            return EbookStatsSyncReport(failed=True)
        current_watermark = f"{file_stat.st_mtime_ns}:{file_stat.st_size}"
        if await self._read_watermark() == current_watermark:
            return EbookStatsSyncReport(skipped=True)
        try:
            parsed = await self._parse_snapshot(source)
        except OSError, sqlite3.Error:
            logger.exception(
                "Failed to parse ebook statistics file",
                path=str(self.statistics_db_path),
            )
            return EbookStatsSyncReport(failed=True)
        book_id_by_source_id = await self._upsert_books(parsed.books)
        events_inserted = await self._insert_events(
            parsed.page_events, book_id_by_source_id
        )
        await self._store_watermark(current_watermark)
        logger.info(
            "Ebook statistics sync completed",
            books_upserted=len(book_id_by_source_id),
            events_inserted=events_inserted,
        )
        return EbookStatsSyncReport(
            books_upserted=len(book_id_by_source_id), events_inserted=events_inserted
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sync passes on the given interval until cancelled.

        Mirrors the other ingestion workers: a failed pass is logged with its
        traceback and the loop survives, so a transient read/parse failure
        (`sync` already turns most of these into a reported failure rather than
        an exception) never takes the worker down.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ebook statistics sync pass failed")

    async def _parse_snapshot(self, source: AsyncPath) -> ParsedStatistics:
        """Copy the source file to a private temp path, parse it, then delete it.

        The live path may be mid-write (Syncthing), so it is never opened
        directly — only this snapshot copy is. The copy is removed whether the
        parse succeeds or raises.
        """
        contents = await source.read_bytes()
        async with NamedTemporaryFile(delete=False) as temp_file:
            temp_path = Path(temp_file.wrapped.name)
            bytes_written = await temp_file.write(contents)
            assert bytes_written == len(contents)
        try:
            return await asyncio.to_thread(parse_statistics_file, temp_path)
        finally:
            await AsyncPath(temp_path).unlink(missing_ok=True)

    async def _upsert_books(self, books: tuple[ParsedBook, ...]) -> dict[int, int]:
        """Upsert every parsed book; returns `source_book_id` -> our row id."""
        book_id_by_source_id: dict[int, int] = {}
        for parsed_book in books:
            row = await self._upsert_book(parsed_book)
            book_id_by_source_id[parsed_book.source_book_id] = row.id
        return book_id_by_source_id

    async def _upsert_book(self, parsed_book: ParsedBook) -> EbookStatBook[Fetched]:
        """Insert or refresh one book row, opportunistically linking `document_hash`.

        A title match sets the link; no match leaves a prior link untouched
        (the link is best-effort and never force-reconciled away).
        """

        async def _upsert(tx: Transaction) -> EbookStatBook[Fetched]:
            existing = await tx.fetch_one_or_none(
                select(EbookStatBook).where(
                    EbookStatBook.source_book_id.eq(parsed_book.source_book_id)
                )
            )
            document_hash = existing.document_hash if existing is not None else None
            if parsed_book.title:
                matched_document = await tx.fetch_one_or_none(
                    select(EbookDocument).where(
                        EbookDocument.title.eq(parsed_book.title)
                    )
                )
                if matched_document is not None:
                    document_hash = matched_document.document_hash
            if existing is None:
                return await tx.execute(
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
            _ = await tx.execute(
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
            return await tx.fetch_one(
                select(EbookStatBook).where(
                    EbookStatBook.source_book_id.eq(parsed_book.source_book_id)
                )
            )

        return await run_in_transaction(self.database, _upsert)

    async def _insert_events(
        self,
        events: tuple[ParsedPageEvent, ...],
        book_id_by_source_id: dict[int, int],
    ) -> int:
        """Insert every page event not already stored under its natural key."""
        inserted = 0
        for source_book_id, book_events in _group_events_by_book(events).items():
            book_id = book_id_by_source_id.get(source_book_id)
            if book_id is None:
                continue
            inserted += await self._insert_book_events(book_id, book_events)
        return inserted

    async def _insert_book_events(
        self, book_id: int, events: Iterable[ParsedPageEvent]
    ) -> int:
        """Insert one book's new page events, skipping any natural-key duplicate."""
        existing_keys = await self._existing_event_keys(book_id)

        async def _insert(tx: Transaction) -> int:
            count = 0
            for event in events:
                key = (event.page, event.start_time)
                if key in existing_keys:
                    continue
                _ = await tx.execute(
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
                count += 1
            return count

        return await run_in_transaction(self.database, _insert)

    async def _existing_event_keys(self, book_id: int) -> set[tuple[int, int]]:
        """The `(page, start_time)` pairs already stored for a book."""
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(
                select(EbookStatPageEvent).where(EbookStatPageEvent.book.eq(book_id))
            )
        return {(row.page, row.start_time) for row in rows}

    async def _read_watermark(self) -> str | None:
        """The last fully-ingested file's `mtime_ns:size`, or `None` on first sync."""
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(EbookStatSyncState).where(
                    EbookStatSyncState.key.eq(_WATERMARK_KEY)
                )
            )
        return row.value if row is not None else None

    async def _store_watermark(self, watermark: str) -> None:
        """Persist the watermark, upserting the single sync-state row."""

        async def _set(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(EbookStatSyncState).where(
                    EbookStatSyncState.key.eq(_WATERMARK_KEY)
                )
            )
            if existing is None:
                _ = await tx.execute(
                    insert(EbookStatSyncState(key=_WATERMARK_KEY, value=watermark))
                )
            else:
                _ = await tx.execute(
                    update(EbookStatSyncState)
                    .set(EbookStatSyncState.value.to(watermark))
                    .where(EbookStatSyncState.key.eq(_WATERMARK_KEY))
                )

        await run_in_transaction(self.database, _set)


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


__all__ = [
    "EbookStatBook",
    "EbookStatPageEvent",
    "EbookStatSyncState",
    "EbookStatsSyncReport",
    "EbookStatsSyncService",
    "ParsedBook",
    "ParsedPageEvent",
    "ParsedStatistics",
    "create_ebook_stats_schema",
    "parse_statistics_file",
]
