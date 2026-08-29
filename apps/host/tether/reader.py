"""Readwise Reader progress ingestion into canonical ebook Evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from snekok.result import Err, Ok, Result
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.kosync_store import EbookDocument, EbookProgressEvent
from tether.readwise_http import (
    ReaderTransport,
    ReadwiseAuthenticationFailure,
    ReadwiseFailure,
    ReadwiseHttpFailure,
    ReadwiseNetworkFailure,
    ReadwiseProtocolFailure,
    ReadwiseRateLimitFailure,
    ReadwiseResponse,
)
from tether.readwise_store import (
    READER_WATERMARK_KEY,
    read_sync_watermark,
    write_sync_watermark,
)
from tether.structured_logging import Logger

_RATE_LIMITED_STATUS = 429
_SUCCESS_STATUS_MIN = 200
_SUCCESS_STATUS_MAX = 300
_READER_ARCHIVE_LOCATION = "archive"
_READER_CATEGORIES = ("epub", "pdf")
_READER_DEVICE = "readwise-reader"
_READER_FINISHED_THRESHOLD = 0.98


@dataclass(frozen=True, slots=True)
class ReaderDocument:
    """One parsed Reader document and its latest progress state."""

    author: str
    category: str
    document_id: str
    location: str
    read_at: datetime | None
    reading_progress: float
    title: str


@dataclass(frozen=True, slots=True)
class ReaderSyncReport:
    """How each Reader document resolved during one pass."""

    appended: int = 0
    finished: int = 0
    skipped: int = 0


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _parse_datetime(raw: object) -> datetime | None:
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


def _parse_reader_document(raw: Mapping[str, object]) -> ReaderDocument | None:
    document_id = raw.get("id")
    if not isinstance(document_id, str) or not document_id:
        return None
    reading_progress = raw.get("reading_progress")
    return ReaderDocument(
        author=_string_field(raw, "author"),
        category=_string_field(raw, "category"),
        document_id=document_id,
        location=_string_field(raw, "location"),
        read_at=_parse_datetime(raw.get("last_opened_at"))
        or _parse_datetime(raw.get("updated_at")),
        reading_progress=(
            float(reading_progress)
            if isinstance(reading_progress, int | float)
            else 0.0
        ),
        title=_string_field(raw, "title"),
    )


class ReaderClient:
    """Paginate and validate Reader documents for each supported category."""

    def __init__(
        self,
        transport: ReaderTransport,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 5,
    ) -> None:
        self.transport: ReaderTransport = transport
        self.sleep: Callable[[float], Awaitable[None]] = sleep
        self.max_retries: int = max_retries

    async def fetch_documents(  # noqa: PLR0911 - each provider failure exits explicitly
        self, *, updated_after: datetime | None, logger: Logger
    ) -> Result[list[ReaderDocument], ReadwiseFailure]:
        """Fetch and validate every epub and PDF page."""
        documents: list[ReaderDocument] = []
        for category in _READER_CATEGORIES:
            page_cursor: str | None = None
            while True:
                page = await self._fetch_page(
                    updated_after=updated_after,
                    category=category,
                    page_cursor=page_cursor,
                    logger=logger,
                )
                if isinstance(page, Err):
                    return Err(page.error)
                response = page.unwrap()
                if response.status_code in {401, 403}:
                    return Err(
                        ReadwiseAuthenticationFailure(
                            operation="list", status_code=response.status_code
                        )
                    )
                if (
                    not _SUCCESS_STATUS_MIN
                    <= response.status_code
                    < _SUCCESS_STATUS_MAX
                ):
                    return Err(
                        ReadwiseHttpFailure(
                            operation="list",
                            retry_after=response.retry_after,
                            status_code=response.status_code,
                        )
                    )
                results = response.payload.get("results")
                if not isinstance(results, list):
                    return Err(ReadwiseProtocolFailure(operation="list"))
                result_entries = cast("list[object]", results)
                if any(not isinstance(entry, Mapping) for entry in result_entries):
                    return Err(ReadwiseProtocolFailure(operation="list"))
                documents.extend(
                    document
                    for entry in result_entries
                    if (
                        document := _parse_reader_document(
                            cast("Mapping[str, object]", entry)
                        )
                    )
                    is not None
                )
                next_cursor = response.payload.get("nextPageCursor")
                if next_cursor is not None and not isinstance(next_cursor, str):
                    return Err(ReadwiseProtocolFailure(operation="list"))
                page_cursor = next_cursor
                if not page_cursor:
                    break
        return Ok(documents)

    async def _fetch_page(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
        logger: Logger,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure | ReadwiseRateLimitFailure]:
        retry_after: timedelta | None = None
        for _ in range(self.max_retries):
            transport_response = await self.transport.fetch_list(
                updated_after=updated_after,
                category=category,
                page_cursor=page_cursor,
            )
            if isinstance(transport_response, Err):
                return transport_response
            response = transport_response.unwrap()
            if response.status_code != _RATE_LIMITED_STATUS:
                return Ok(response)
            retry_after = response.retry_after
            delay = retry_after.total_seconds() if retry_after is not None else 1.0
            _info(logger, "Reader rate limited; backing off", delay_seconds=delay)
            await self.sleep(delay)
        return Err(ReadwiseRateLimitFailure(operation="list", retry_after=retry_after))


class ReaderSyncService:
    """Append Reader progress and derive one source completion per document."""

    def __init__(self, database: Database, client: ReaderClient) -> None:
        self.database: Database = database
        self.client: ReaderClient = client

    async def sync(
        self, *, logger: Logger
    ) -> Result[ReaderSyncReport, ReadwiseFailure]:
        """Run one pass and advance the cursor only after full success."""
        started_at = datetime.now(UTC)
        watermark = await read_sync_watermark(self.database, READER_WATERMARK_KEY)
        _debug(
            logger,
            "Reader sync starting",
            incremental=watermark is not None,
            updated_after=watermark.isoformat() if watermark is not None else None,
        )
        documents = await self.client.fetch_documents(
            updated_after=watermark, logger=logger
        )
        if isinstance(documents, Err):
            return Err(documents.error)
        appended = skipped = finished = 0
        for document in documents.unwrap():
            outcome = await self._apply_document(document)
            if outcome == "appended":
                appended += 1
            elif outcome == "finished":
                finished += 1
            else:
                skipped += 1
        await write_sync_watermark(self.database, READER_WATERMARK_KEY, started_at)
        _info(
            logger,
            "Reader sync completed",
            appended=appended,
            skipped=skipped,
            finished=finished,
        )
        return Ok(
            ReaderSyncReport(appended=appended, finished=finished, skipped=skipped)
        )

    async def _apply_document(self, document: ReaderDocument) -> str:
        key = f"reader:{document.document_id}"
        stored = await self._upsert_document(key, document.title)
        appended = await self._append_if_changed(key, document)
        if self._is_finished(document) and stored.finished_at is None:
            await self._stamp_finished(key)
            return "finished"
        return "appended" if appended else "skipped"

    @staticmethod
    def _is_finished(document: ReaderDocument) -> bool:
        return (
            document.location == _READER_ARCHIVE_LOCATION
            or document.reading_progress >= _READER_FINISHED_THRESHOLD
        )

    async def _upsert_document(self, key: str, title: str) -> EbookDocument[Fetched]:
        async def _upsert(transaction: Transaction) -> EbookDocument[Fetched]:
            existing = await transaction.fetch_one_or_none(
                select(EbookDocument).where(EbookDocument.document_hash.eq(key))
            )
            if existing is None:
                return await transaction.execute(
                    insert(
                        EbookDocument(document_hash=key, title=title or None)
                    ).returning()
                )
            _ = await transaction.execute(
                update(EbookDocument)
                .set(
                    EbookDocument.title.to(title or None),
                    EbookDocument.updated_at.to(CurrentTimestamp),
                )
                .where(EbookDocument.document_hash.eq(key))
            )
            return existing

        async with self.database.transaction(mode="immediate") as transaction:
            return await _upsert(transaction)

    async def _append_if_changed(self, key: str, document: ReaderDocument) -> bool:
        latest = await self._latest_event(key)
        if (
            latest is not None
            and latest.percentage == document.reading_progress
            and latest.progress == document.location
        ):
            return False
        await self._append_event(key, document)
        return True

    async def _latest_event(self, key: str) -> EbookProgressEvent[Fetched] | None:
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(EbookProgressEvent)
                .where(EbookProgressEvent.document_hash.eq(key))
                .order_by(EbookProgressEvent.id.desc())
                .limit(1)
            )

    async def _append_event(self, key: str, document: ReaderDocument) -> None:
        read_at = document.read_at or datetime.now(UTC)
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                insert(
                    EbookProgressEvent(
                        document_hash=key,
                        percentage=document.reading_progress,
                        progress=document.location,
                        device=_READER_DEVICE,
                        device_id="",
                        timestamp=int(read_at.timestamp()),
                    )
                )
            )

    async def _stamp_finished(self, key: str) -> None:
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(EbookDocument)
                .set(EbookDocument.finished_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(key))
            )


__all__ = [
    "ReaderClient",
    "ReaderDocument",
    "ReaderSyncReport",
    "ReaderSyncService",
]
