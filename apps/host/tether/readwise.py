"""Readwise Export ingestion into canonical highlight Evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from snekok import Err, Ok, Result
from snekql.sqlite import Database, Fetched, insert, select, update

from tether.readwise_http import (
    ReadwiseAuthenticationFailure,
    ReadwiseFailure,
    ReadwiseHttpFailure,
    ReadwiseNetworkFailure,
    ReadwiseProtocolFailure,
    ReadwiseRateLimitFailure,
    ReadwiseResponse,
    ReadwiseTransport,
)
from tether.readwise_store import (
    HIGHLIGHTS_WATERMARK_KEY,
    ReadwiseHighlight,
    read_sync_watermark,
    write_sync_watermark,
)
from tether.structured_logging import Logger

_AUTH_OK_STATUS = 204
_RATE_LIMITED_STATUS = 429
_SUCCESS_STATUS_MIN = 200
_SUCCESS_STATUS_MAX = 300


@dataclass(frozen=True, slots=True)
class ReadwiseHighlightRecord:
    """One parsed highlight carrying its upstream identity and edit state."""

    highlight_id: int
    is_deleted: bool
    is_discard: bool
    note: str
    tags: tuple[str, ...]
    text: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReadwiseBook:
    """Book-level facets and the highlights exported beneath them."""

    author: str
    category: str
    highlights: tuple[ReadwiseHighlightRecord, ...]
    readable_title: str


@dataclass(frozen=True, slots=True)
class ReadwiseSyncReport:
    """How each exported highlight resolved during one pass."""

    created: int = 0
    deleted: int = 0
    skipped: int = 0
    updated: int = 0


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _parse_datetime(raw: object) -> datetime | None:
    """Parse an ISO timestamp, tolerating a trailing `Z` and malformed input."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _string_field(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _parse_tags(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for tag in cast("list[object]", raw):
        if isinstance(tag, Mapping):
            name = cast("Mapping[str, object]", tag).get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return tuple(names)


def _parse_highlight(raw: Mapping[str, object]) -> ReadwiseHighlightRecord | None:
    highlight_id = raw.get("id")
    if not isinstance(highlight_id, int):
        return None
    text = raw.get("text")
    note = raw.get("note")
    return ReadwiseHighlightRecord(
        highlight_id=highlight_id,
        is_deleted=raw.get("is_deleted") is True,
        is_discard=raw.get("is_discard") is True,
        note=note.strip() if isinstance(note, str) else "",
        tags=_parse_tags(raw.get("tags")),
        text=text if isinstance(text, str) else "",
        updated_at=_parse_datetime(raw.get("updated_at")),
    )


def _parse_book(raw: Mapping[str, object]) -> ReadwiseBook:
    highlights_raw = raw.get("highlights")
    highlights: list[ReadwiseHighlightRecord] = []
    if isinstance(highlights_raw, list):
        for entry in cast("list[object]", highlights_raw):
            if isinstance(entry, Mapping):
                parsed = _parse_highlight(cast("Mapping[str, object]", entry))
                if parsed is not None:
                    highlights.append(parsed)
    return ReadwiseBook(
        author=_string_field(raw, "author"),
        category=_string_field(raw, "category"),
        highlights=tuple(highlights),
        readable_title=_string_field(raw, "readable_title"),
    )


def _highlight_content(highlight: ReadwiseHighlightRecord) -> str:
    if highlight.note:
        return f"{highlight.text}\n\nNote: {highlight.note}"
    return highlight.text


def _highlight_facets(
    book: ReadwiseBook, highlight: ReadwiseHighlightRecord
) -> dict[str, str]:
    facets = {"source": "readwise"}
    if book.readable_title:
        facets["title"] = book.readable_title
    if book.author:
        facets["author"] = book.author
    if book.category:
        facets["category"] = book.category
    if highlight.tags:
        facets["tags"] = ", ".join(highlight.tags)
    return facets


def _isoformat_or_empty(when: datetime | None) -> str:
    return when.isoformat() if when is not None else ""


class ReadwiseClient:
    """Validate token state and paginate typed Readwise Export responses."""

    def __init__(
        self,
        transport: ReadwiseTransport,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 5,
    ) -> None:
        self.transport: ReadwiseTransport = transport
        self.sleep: Callable[[float], Awaitable[None]] = sleep
        self.max_retries: int = max_retries

    async def verify_token(
        self, *, logger: Logger
    ) -> Result[
        None,
        ReadwiseAuthenticationFailure | ReadwiseHttpFailure | ReadwiseNetworkFailure,
    ]:
        """Validate the configured token without raising expected failures."""
        transport_response = await self.transport.verify_token()
        if isinstance(transport_response, Err):
            return Err(transport_response.error)
        response = transport_response.value
        if response.status_code == _AUTH_OK_STATUS:
            return Ok(None)
        _info(logger, "Readwise token check failed", status_code=response.status_code)
        if response.status_code in {401, 403}:
            return Err(
                ReadwiseAuthenticationFailure(
                    operation="verify-token", status_code=response.status_code
                )
            )
        return Err(
            ReadwiseHttpFailure(
                operation="verify-token",
                retry_after=response.retry_after,
                status_code=response.status_code,
            )
        )

    async def fetch_export(  # noqa: PLR0911 - each provider failure exits explicitly
        self,
        *,
        updated_after: datetime | None,
        include_deleted: bool,
        logger: Logger,
    ) -> Result[list[ReadwiseBook], ReadwiseFailure]:
        """Fetch and validate every Export page."""
        books: list[ReadwiseBook] = []
        page_cursor: str | None = None
        while True:
            page = await self._fetch_page(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
                logger=logger,
            )
            if isinstance(page, Err):
                return Err(page.error)
            response = page.value
            if response.status_code in {401, 403}:
                return Err(
                    ReadwiseAuthenticationFailure(
                        operation="export", status_code=response.status_code
                    )
                )
            if not _SUCCESS_STATUS_MIN <= response.status_code < _SUCCESS_STATUS_MAX:
                return Err(
                    ReadwiseHttpFailure(
                        operation="export",
                        retry_after=response.retry_after,
                        status_code=response.status_code,
                    )
                )
            results = response.payload.get("results")
            if not isinstance(results, list):
                return Err(ReadwiseProtocolFailure(operation="export"))
            result_entries = cast("list[object]", results)
            if any(not isinstance(entry, Mapping) for entry in result_entries):
                return Err(ReadwiseProtocolFailure(operation="export"))
            books.extend(
                _parse_book(cast("Mapping[str, object]", entry))
                for entry in result_entries
            )
            next_cursor = response.payload.get("nextPageCursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                return Err(ReadwiseProtocolFailure(operation="export"))
            page_cursor = next_cursor
            if not page_cursor:
                return Ok(books)

    async def _fetch_page(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
        logger: Logger,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure | ReadwiseRateLimitFailure]:
        retry_after: timedelta | None = None
        for _ in range(self.max_retries):
            transport_response = await self.transport.fetch_export(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
            )
            if isinstance(transport_response, Err):
                return transport_response
            response = transport_response.value
            if response.status_code != _RATE_LIMITED_STATUS:
                return Ok(response)
            retry_after = response.retry_after
            delay = retry_after.total_seconds() if retry_after is not None else 1.0
            _info(logger, "Readwise rate limited; backing off", delay_seconds=delay)
            await self.sleep(delay)
        return Err(
            ReadwiseRateLimitFailure(operation="export", retry_after=retry_after)
        )


class ReadwiseSyncService:
    """Mirror exported highlights into source-owned Evidence idempotently."""

    def __init__(self, database: Database, client: ReadwiseClient) -> None:
        self.database: Database = database
        self.client: ReadwiseClient = client

    async def sync(
        self, *, logger: Logger
    ) -> Result[ReadwiseSyncReport, ReadwiseFailure]:
        """Run one pass and advance the cursor only after full success."""
        started_at = datetime.now(UTC)
        watermark = await read_sync_watermark(self.database, HIGHLIGHTS_WATERMARK_KEY)
        include_deleted = watermark is not None
        _debug(
            logger,
            "Readwise sync starting",
            incremental=include_deleted,
            updated_after=watermark.isoformat() if watermark is not None else None,
        )
        export = await self.client.fetch_export(
            updated_after=watermark,
            include_deleted=include_deleted,
            logger=logger,
        )
        if isinstance(export, Err):
            return Err(export.error)
        created = updated = deleted = skipped = 0
        for book in export.value:
            for highlight in book.highlights:
                outcome = await self._apply_highlight(book, highlight, logger=logger)
                if outcome == "created":
                    created += 1
                elif outcome == "updated":
                    updated += 1
                elif outcome == "deleted":
                    deleted += 1
                else:
                    skipped += 1
        await write_sync_watermark(self.database, HIGHLIGHTS_WATERMARK_KEY, started_at)
        _info(
            logger,
            "Readwise sync completed",
            created=created,
            updated=updated,
            deleted=deleted,
            skipped=skipped,
        )
        return Ok(
            ReadwiseSyncReport(
                created=created,
                deleted=deleted,
                skipped=skipped,
                updated=updated,
            )
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run periodic passes until cancellation."""
        while True:
            await asyncio.sleep(interval_seconds)
            report = await self.sync(logger=logger)
            if isinstance(report, Err):
                logger.warning(
                    "Readwise sync pass failed",
                    failure=type(report.error).__name__,
                    operation=report.error.operation,
                )

    async def _apply_highlight(
        self,
        book: ReadwiseBook,
        highlight: ReadwiseHighlightRecord,
        *,
        logger: Logger,
    ) -> str:
        mapping = await self._fetch_mapping(highlight.highlight_id)
        if highlight.is_deleted or highlight.is_discard:
            if mapping is None:
                return "skipped"
            await self._delete_highlight(mapping, logger=logger)
            return "deleted"
        return await self._upsert_highlight(book, highlight, mapping)

    async def _upsert_highlight(
        self,
        book: ReadwiseBook,
        highlight: ReadwiseHighlightRecord,
        mapping: ReadwiseHighlight[Fetched] | None,
    ) -> str:
        content = _highlight_content(highlight)
        if not content.strip():
            return "skipped"
        facets = _highlight_facets(book, highlight)
        if mapping is None:
            await self._create_highlight(highlight, content, facets)
            return "created"
        if not self._is_newer(highlight, mapping):
            return "skipped"
        await self._edit_highlight(mapping, highlight, content, facets)
        return "updated"

    async def _create_highlight(
        self,
        highlight: ReadwiseHighlightRecord,
        content: str,
        metadata: dict[str, str],
    ) -> None:
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                insert(
                    ReadwiseHighlight(
                        highlight_id=highlight.highlight_id,
                        content=content,
                        metadata=metadata,
                        updated_at=_isoformat_or_empty(highlight.updated_at),
                    )
                )
            )

    async def _edit_highlight(
        self,
        mapping: ReadwiseHighlight[Fetched],
        highlight: ReadwiseHighlightRecord,
        content: str,
        metadata: dict[str, str],
    ) -> None:
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(ReadwiseHighlight)
                .set(
                    ReadwiseHighlight.content.to(content),
                    ReadwiseHighlight.metadata.to(metadata),
                    ReadwiseHighlight.updated_at.to(
                        _isoformat_or_empty(highlight.updated_at)
                    ),
                )
                .where(ReadwiseHighlight.highlight_id.eq(mapping.highlight_id))
            )

    async def _delete_highlight(
        self, mapping: ReadwiseHighlight[Fetched], *, logger: Logger
    ) -> None:
        _ = logger
        async with self.database.transaction(mode="immediate") as transaction:
            connection = transaction.require_connection()
            cursor = await connection.execute(
                'DELETE FROM "readwise_highlight" WHERE "highlight_id" = ?',
                (mapping.highlight_id,),
            )
            await cursor.close()

    @staticmethod
    def _is_newer(
        highlight: ReadwiseHighlightRecord, mapping: ReadwiseHighlight[Fetched]
    ) -> bool:
        if highlight.updated_at is None:
            return False
        stored = _parse_datetime(mapping.updated_at)
        return stored is None or highlight.updated_at > stored

    async def _fetch_mapping(
        self, highlight_id: int
    ) -> ReadwiseHighlight[Fetched] | None:
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(ReadwiseHighlight).where(
                    ReadwiseHighlight.highlight_id.eq(highlight_id)
                )
            )


__all__ = [
    "ReadwiseBook",
    "ReadwiseClient",
    "ReadwiseHighlightRecord",
    "ReadwiseSyncReport",
    "ReadwiseSyncService",
]
