"""KOReader statistics-file snapshot and ingestion orchestration."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from anyio import NamedTemporaryFile
from anyio import Path as AsyncPath
from snekok.result import Err, Ok, Result

from tether.ebook_stats_errors import (
    EbookStatsFailure,
    EbookStatsParseFailure,
    EbookStatsSourceFailure,
)
from tether.ebook_stats_model import EbookStatsSyncReport, ParsedStatistics
from tether.ebook_stats_parser import parse_statistics_file
from tether.ebook_stats_store import EbookStatsStore
from tether.structured_logging import Logger


class EbookStatsSyncService:
    """Ingest changed KOReader statistics snapshots into canonical SQLite.

    The live Syncthing path is copied before foreign SQLite parsing. The
    persisted watermark advances only after every parsed book and page event
    has been stored successfully.

    >>> service = EbookStatsSyncService(store=store, statistics_db_path=path)
    >>> result = await service.sync(logger=logger)
    >>> isinstance(result, Ok)
    True
    """

    def __init__(self, store: EbookStatsStore, statistics_db_path: Path) -> None:
        self.statistics_db_path: Path = statistics_db_path
        self.store: EbookStatsStore = store

    async def sync(
        self, *, logger: Logger
    ) -> Result[EbookStatsSyncReport, EbookStatsFailure]:
        """Run one pass, returning expected source and parse failures as values."""
        source = AsyncPath(self.statistics_db_path)
        try:
            file_stat = await source.stat()
        except OSError:
            logger.warning(
                "Ebook statistics file not found",
                path=str(self.statistics_db_path),
            )
            return Err(
                EbookStatsSourceFailure(
                    operation="stat", path=str(self.statistics_db_path)
                )
            )
        current_watermark = f"{file_stat.st_mtime_ns}:{file_stat.st_size}"
        if await self.store.read_watermark() == current_watermark:
            return Ok(EbookStatsSyncReport(skipped=True))
        try:
            parsed = await self._parse_snapshot(source)
        except OSError:
            logger.exception(
                "Failed to snapshot ebook statistics file",
                path=str(self.statistics_db_path),
            )
            return Err(
                EbookStatsSourceFailure(
                    operation="snapshot", path=str(self.statistics_db_path)
                )
            )
        except sqlite3.Error:
            logger.exception(
                "Failed to parse ebook statistics file",
                path=str(self.statistics_db_path),
            )
            return Err(EbookStatsParseFailure(path=str(self.statistics_db_path)))
        book_id_by_source_id = await self.store.upsert_books(parsed.books)
        events_inserted = await self.store.insert_events(
            parsed.page_events, book_id_by_source_id
        )
        await self.store.write_watermark(current_watermark)
        report = EbookStatsSyncReport(
            books_upserted=len(book_id_by_source_id),
            events_inserted=events_inserted,
        )
        logger.info(
            "Ebook statistics sync completed",
            books_upserted=report.books_upserted,
            events_inserted=report.events_inserted,
        )
        return Ok(report)

    async def _parse_snapshot(self, source: AsyncPath) -> ParsedStatistics:
        """Copy the potentially live source, parse off-thread, then remove it."""
        contents = await source.read_bytes()
        async with NamedTemporaryFile(delete=False) as temp_file:
            temp_path = Path(temp_file.wrapped.name)
            bytes_written = await temp_file.write(contents)
            assert bytes_written == len(contents)
        try:
            return await asyncio.to_thread(parse_statistics_file, temp_path)
        finally:
            await AsyncPath(temp_path).unlink(missing_ok=True)
