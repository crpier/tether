"""Domain values parsed and reported by KOReader statistics ingestion."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedBook:
    """One `book` row read from a `statistics.sqlite` snapshot."""

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
    """One `page_stat_data` row read from a statistics snapshot."""

    duration: int
    page: int
    source_book_id: int
    start_time: int


@dataclass(frozen=True, slots=True)
class ParsedStatistics:
    """The complete domain parse of one statistics snapshot."""

    books: tuple[ParsedBook, ...]
    page_events: tuple[ParsedPageEvent, ...]


@dataclass(frozen=True, slots=True)
class EbookStatsSyncReport:
    """Counts from a successful pass, or an unchanged-file skip."""

    books_upserted: int = 0
    events_inserted: int = 0
    skipped: bool = False
