"""Behaviour tests for KOReader `statistics.sqlite` ingestion.

These drive `EbookStatsSyncService` against a real in-memory tether database
and a real, hand-built KOReader-schema `statistics.sqlite` fixture file on
disk (no binary fixture checked in) — never a live device. They assert the
book/page-event parse, the mtime/size watermark gate, idempotency of
re-parsing, opportunistic `document_hash` title-linking, and that a missing or
malformed source file degrades to a logged failure rather than an exception.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import structlog
from anyio import Path as AsyncPath
from anyio import TemporaryDirectory
from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import (
    assert_eq,
    assert_false,
    assert_is_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.db_retry import run_in_transaction
from tether.ebook_stats import (
    EbookStatBook,
    EbookStatPageEvent,
    EbookStatsSyncService,
    create_ebook_stats_schema,
)
from tether.kosync import EbookDocument, create_kosync_schema
from tether.logging import Logger


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.ebook_stats")


@dataclass(frozen=True, slots=True)
class StatsBookRow:
    """One `book` row to seed into a fixture `statistics.sqlite`."""

    id: int
    title: str
    authors: str = ""
    md5: str = "deadbeef"
    total_read_time: int = 0
    total_read_pages: int = 0
    highlights: int = 0
    notes: int = 0
    last_open: int = 0
    pages: int = 100


@dataclass(frozen=True, slots=True)
class StatsPageEventRow:
    """One `page_stat_data` row to seed into a fixture `statistics.sqlite`."""

    id_book: int
    page: int
    start_time: int
    duration: int = 10


def write_statistics_file(
    path: Path,
    *,
    books: tuple[StatsBookRow, ...] = (),
    page_events: tuple[StatsPageEventRow, ...] = (),
) -> None:
    """Build a real KOReader-schema `statistics.sqlite` at `path`.

    Mirrors the actual on-device schema conservatively: `book` carries id,
    title, authors, md5, the read-total/annotation counters, and pages;
    `page_stat_data` carries id_book/page/start_time/duration.
    """
    create_book_table_sql = (
        "CREATE TABLE book ("
        "id INTEGER PRIMARY KEY, title TEXT, authors TEXT, notes INTEGER, "
        "last_open INTEGER, highlights INTEGER, pages INTEGER, "
        "series TEXT, language TEXT, md5 TEXT, "
        "total_read_time INTEGER, total_read_pages INTEGER)"
    )
    create_page_stat_data_table_sql = (
        "CREATE TABLE page_stat_data ("
        "id_book INTEGER, page INTEGER, start_time INTEGER, "
        "duration INTEGER, total_pages INTEGER)"
    )
    insert_book_sql = (
        "INSERT INTO book (id, title, authors, notes, last_open, "
        "highlights, pages, md5, total_read_time, total_read_pages) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_page_stat_data_sql = (
        "INSERT INTO page_stat_data "
        "(id_book, page, start_time, duration) VALUES (?, ?, ?, ?)"
    )
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(create_book_table_sql)
        connection.execute(create_page_stat_data_table_sql)
        for book in books:
            connection.execute(
                insert_book_sql,
                (
                    book.id,
                    book.title,
                    book.authors,
                    book.notes,
                    book.last_open,
                    book.highlights,
                    book.pages,
                    book.md5,
                    book.total_read_time,
                    book.total_read_pages,
                ),
            )
        for event in page_events:
            connection.execute(
                insert_page_stat_data_sql,
                (event.id_book, event.page, event.start_time, event.duration),
            )
        connection.commit()
    finally:
        connection.close()


@dataclass
class EbookStatsEnv:
    """A schema-ready database plus a temp dir to hold statistics files."""

    database: Database
    tmp_dir: Path
    logger: Logger

    def service_for(self, filename: str = "statistics.sqlite") -> EbookStatsSyncService:
        """A service pointed at `<tmp_dir>/<filename>` (need not exist yet)."""
        return EbookStatsSyncService(
            database=self.database, statistics_db_path=self.tmp_dir / filename
        )

    async def all_books(self) -> list[EbookStatBook[Fetched]]:
        async with self.database.transaction() as tx:
            return await tx.fetch_all(select(EbookStatBook).all())

    async def all_page_events(self) -> list[EbookStatPageEvent[Fetched]]:
        async with self.database.transaction() as tx:
            return await tx.fetch_all(select(EbookStatPageEvent).all())


@fixture
async def ebook_stats_env() -> AsyncGenerator[EbookStatsEnv]:
    """A fresh in-memory database with the ebook-stats + kosync schema."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_ebook_stats_schema(database)
    await create_kosync_schema(database)
    async with TemporaryDirectory() as tmp_dir:
        yield EbookStatsEnv(
            database=database, tmp_dir=Path(tmp_dir), logger=test_logger()
        )
    await database.close()


@test()
async def a_full_parse_lands_book_rows() -> None:
    """Parsing a fixture file stores one `EbookStatBook` row per `book` row."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work", authors="Cal Newport"),),
    )

    _ = await env.service_for().sync(logger=env.logger)

    books = await env.all_books()
    assert_eq(len(books), 1)
    assert_eq(books[0].title, "Deep Work")
    assert_eq(books[0].authors, "Cal Newport")


@test()
async def a_full_parse_lands_page_event_rows() -> None:
    """Parsing a fixture file stores one `EbookStatPageEvent` per page row."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work"),),
        page_events=(
            StatsPageEventRow(id_book=1, page=1, start_time=1000, duration=30),
            StatsPageEventRow(id_book=1, page=2, start_time=1030, duration=45),
        ),
    )

    _ = await env.service_for().sync(logger=env.logger)

    events = await env.all_page_events()
    assert_eq(len(events), 2)
    assert_eq({event.page for event in events}, {1, 2})


@test()
async def report_counts_reflect_a_successful_parse() -> None:
    """The sync report tallies the books and events a successful pass stored."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work"),),
        page_events=(StatsPageEventRow(id_book=1, page=1, start_time=1000),),
    )

    report = await env.service_for().sync(logger=env.logger)

    assert_eq(report.books_upserted, 1)
    assert_eq(report.events_inserted, 1)
    assert_false(report.skipped)
    assert_false(report.failed)


@test()
async def an_unchanged_file_is_skipped_on_the_next_tick() -> None:
    """A second sync against an unchanged mtime/size does not re-parse."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work"),),
    )
    service = env.service_for()
    _ = await service.sync(logger=env.logger)

    second_report = await service.sync(logger=env.logger)

    assert_true(second_report.skipped)


@test()
async def an_unchanged_file_leaves_stored_rows_untouched() -> None:
    """Skipping an unchanged file does not touch previously stored rows."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work"),),
    )
    service = env.service_for()
    _ = await service.sync(logger=env.logger)

    _ = await service.sync(logger=env.logger)

    assert_eq(len(await env.all_books()), 1)


@test()
async def a_changed_file_is_reparsed() -> None:
    """A file whose mtime/size changed since the watermark is re-parsed."""
    env = await load_fixture(ebook_stats_env())
    statistics_path = env.tmp_dir / "statistics.sqlite"
    write_statistics_file(
        statistics_path, books=(StatsBookRow(id=1, title="Deep Work"),)
    )
    service = env.service_for()
    _ = await service.sync(logger=env.logger)
    statistics_path.unlink()
    write_statistics_file(
        statistics_path,
        books=(
            StatsBookRow(id=1, title="Deep Work"),
            StatsBookRow(id=2, title="Digital Minimalism"),
        ),
    )

    report = await service.sync(logger=env.logger)

    assert_false(report.skipped)
    assert_eq(len(await env.all_books()), 2)


@test()
async def reparsing_the_same_snapshot_does_not_duplicate_events() -> None:
    """Re-running a parse over the same rows inserts no duplicate page events.

    Forces a re-parse by writing an identical file to a fresh path (a new
    mtime/size relative to the stored watermark), exercising the natural-key
    idempotency guard independent of the watermark gate.
    """
    env = await load_fixture(ebook_stats_env())
    statistics_path = env.tmp_dir / "statistics.sqlite"
    write_statistics_file(
        statistics_path,
        books=(StatsBookRow(id=1, title="Deep Work"),),
        page_events=(StatsPageEventRow(id_book=1, page=1, start_time=1000),),
    )
    service = env.service_for()
    _ = await service.sync(logger=env.logger)
    # Force a re-parse of the identical rows by resetting the stored watermark.
    _ = await service._store_watermark("stale")

    _ = await service.sync(logger=env.logger)

    events = await env.all_page_events()
    assert_eq(len(events), 1)


@test()
async def a_title_match_links_document_hash() -> None:
    """A book whose title matches an `EbookDocument.title` gets `document_hash`."""
    env = await load_fixture(ebook_stats_env())

    _ = await run_in_transaction(
        env.database,
        lambda tx: tx.execute(
            insert(EbookDocument(document_hash="hash-abc", title="Deep Work"))
        ),
    )
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="Deep Work"),),
    )

    _ = await env.service_for().sync(logger=env.logger)

    books = await env.all_books()
    assert_eq(books[0].document_hash, "hash-abc")


@test()
async def an_unmatched_title_leaves_document_hash_null() -> None:
    """A book whose title matches no `EbookDocument` gets a null `document_hash`."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(StatsBookRow(id=1, title="An Unmatched Title"),),
    )

    _ = await env.service_for().sync(logger=env.logger)

    books = await env.all_books()
    assert_is_none(books[0].document_hash)


@test()
async def a_missing_source_file_reports_failure_without_raising() -> None:
    """A never-created source file does not raise; it reports a failure."""
    env = await load_fixture(ebook_stats_env())

    report = await env.service_for("does-not-exist.sqlite").sync(logger=env.logger)

    assert_true(report.failed)


@test()
async def a_malformed_source_file_reports_failure_without_raising() -> None:
    """A file that is not valid sqlite does not raise; it reports a failure."""
    env = await load_fixture(ebook_stats_env())
    malformed_path = env.tmp_dir / "statistics.sqlite"
    _ = await AsyncPath(malformed_path).write_bytes(b"not a sqlite file")

    report = await env.service_for().sync(logger=env.logger)

    assert_true(report.failed)


@test()
async def a_malformed_source_file_does_not_advance_the_watermark() -> None:
    """A failed parse leaves no watermark, so the next tick retries."""
    env = await load_fixture(ebook_stats_env())
    malformed_path = env.tmp_dir / "statistics.sqlite"
    _ = await AsyncPath(malformed_path).write_bytes(b"not a sqlite file")
    service = env.service_for()
    _ = await service.sync(logger=env.logger)

    second_report = await service.sync(logger=env.logger)

    assert_false(second_report.skipped)
    assert_true(second_report.failed)


@test()
async def a_book_missing_from_the_snapshot_yields_no_page_events() -> None:
    """A page-event row whose book id has no matching book row is dropped."""
    env = await load_fixture(ebook_stats_env())
    write_statistics_file(
        env.tmp_dir / "statistics.sqlite",
        books=(),
        page_events=(StatsPageEventRow(id_book=999, page=1, start_time=1000),),
    )

    _ = await env.service_for().sync(logger=env.logger)

    assert_eq(await env.all_page_events(), [])
