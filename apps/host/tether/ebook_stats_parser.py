"""Read-only parser for foreign KOReader `statistics.sqlite` snapshots."""

import sqlite3
from pathlib import Path

from tether.ebook_stats_model import ParsedBook, ParsedPageEvent, ParsedStatistics

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
"""Known upstream `book` columns, with only `id` required."""

_PAGE_STAT_COLUMNS: tuple[str, ...] = ("id_book", "page", "start_time", "duration")
"""Required upstream `page_stat_data` columns."""


def _text_or_none(value: object) -> str | None:
    """Return a non-empty foreign string, otherwise `None`."""
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    """Coerce a numeric foreign value to `int`, excluding booleans."""
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, int | float) else None


def _available_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Read the columns a foreign table actually exposes."""
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row["name"]) for row in rows}


def _parse_books(connection: sqlite3.Connection) -> tuple[ParsedBook, ...]:
    """Parse books while tolerating absent optional columns."""
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
    """Parse complete page events and drop malformed foreign rows."""
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
    """Parse a private snapshot through SQLite's read-only immutable mode.

    This synchronous foreign-database boundary must run in an executor when
    called from the event loop.

    >>> statistics = parse_statistics_file(Path("/tmp/snapshot.sqlite"))  # doctest: +SKIP
    >>> statistics.books[0].source_book_id  # doctest: +SKIP
    1
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return ParsedStatistics(
            books=_parse_books(connection), page_events=_parse_page_events(connection)
        )
    finally:
        connection.close()
