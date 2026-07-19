"""Readwise ingestion gate: v2 highlights synced into the Commons as memories.

A scheduled Ingestion gate that pulls Readwise highlights through the v2 Export
API and mirrors each one into the Commons as a single machine-synced Memory,
trusted at insert (never entering Review). The module is three seams:

- `ReadwiseTransport` — the isolated HTTP boundary (one export GET, one auth
  GET), faked in tests so no live Readwise call runs.
- `ReadwiseClient` — pagination over `nextPageCursor`, `Retry-After` handling,
  and the token check, parsing raw payloads into `ReadwiseBook`s.
- `ReadwiseSyncService` — the reconciler-shaped worker: an idempotent `sync`
  pass (boot + periodic loop) that folds each highlight into a create, an edit,
  or a delete against the `readwise_highlight` mapping table, persisting the
  `updatedAfter` watermark only after a fully successful pass.

Alongside the v2 highlight gate lives the **Reader v3 progress rider**
(`ReaderTransport`/`ReaderClient`/`ReaderSyncService`): a scheduled poll of the
Reader v3 list API that folds epub/pdf reading progress into the shared
`ebook_document`/`ebook_progress_event` Telemetry tables (keyed `reader:<id>`)
and mints one machine-synced "Finished reading" Memory per document. It reuses
the same API token and a separate watermark row.

>>> service = ReadwiseSyncService(
...     database=database, client=client, memory_service=memory_service
... )
>>> report = await service.sync(logger=logger)
>>> report.created
1
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

import httpx2
from snekql.sqlite import (
    CurrentTimestamp,
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

from tether.db_retry import run_in_transaction
from tether.kosync import EbookDocument, EbookProgressEvent
from tether.logging import Logger
from tether.memories import Memory, MemoryConflictError, MemoryProvenance, MemoryService

_EXPORT_PATH = "/api/v2/export/"
_AUTH_PATH = "/api/v2/auth/"
_AUTH_OK_STATUS = 204
"""The auth-check status that means the token is valid."""
_RATE_LIMITED_STATUS = 429
"""The export status that triggers a `Retry-After` backoff."""
_DEFAULT_BASE_URL = "https://readwise.io"
_WATERMARK_KEY = "highlights_export_watermark"
"""Sync-state key under which the last fully successful `updatedAfter` cursor is
persisted. Absence means no successful pass has run — the next sync is a full
backfill."""
_LIST_PATH = "/api/v3/list/"
"""The Reader v3 list endpoint the progress rider polls for epub/pdf documents."""
_READER_LIMIT = 100
"""Max page size the Reader v3 list endpoint accepts (`limit`)."""
_READER_CATEGORIES = ("epub", "pdf")
"""The Reader document categories the rider polls. The list endpoint takes a
single `category` value per request, so the client polls once per category."""
_READER_DEVICE = "readwise-reader"
"""The device label stamped on every Reader-sourced `ebook_progress_event`."""
_READER_FINISHED_THRESHOLD = 0.98
"""Reading fraction at or beyond which a Reader document counts as finished."""
_READER_ARCHIVE_LOCATION = "archive"
"""The Reader `location` that marks a document archived (also counts as finished)."""
_READER_WATERMARK_KEY = "reader_list_watermark"
"""Sync-state key for the Reader rider's last fully successful `updatedAfter`
cursor, kept separate from the v2 highlights watermark. Absence means the next
pass is a full paginated pull."""


class ReadwiseConfigurationError(Exception):
    """Raised when the Readwise HTTP transport is built without an API key."""


class ReadwiseAuthError(Exception):
    """Raised when the configured Readwise token fails the auth check."""


@dataclass(frozen=True, slots=True)
class ReadwiseResponse:
    """One Readwise HTTP response, normalized for the pure client logic.

    `payload` is the decoded JSON body (empty for the 204 auth check);
    `retry_after` is any parsed `Retry-After` hint, meaningful only on a 429.
    Keeping the transport's output this small is what lets pagination and the
    highlight mapping be unit-tested without httpx.
    """

    status_code: int
    payload: Mapping[str, object]
    retry_after: timedelta | None = None


class ReadwiseTransport(Protocol):
    """The isolated Readwise HTTP boundary the client drives.

    Two calls: `fetch_export` pulls one export page (cursor-paginated upstream),
    and `verify_token` hits the auth endpoint. Faked in tests so the client's
    pagination and the sync's mapping run offline.
    """

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> ReadwiseResponse:
        """Fetch one export page (a slice of books with nested highlights)."""
        ...

    async def verify_token(self) -> ReadwiseResponse:
        """Hit the auth endpoint; a 204 means the token is valid."""
        ...


@dataclass(frozen=True, slots=True)
class ReadwiseHighlightRecord:
    """One Readwise highlight, parsed from an export payload.

    `highlight_id` is Readwise's stable integer id — the idempotency key the
    mapping table is keyed on. `updated_at` bumps on every edit upstream, so it
    is what distinguishes an unchanged re-export from a genuine edit.
    """

    highlight_id: int
    text: str
    note: str
    tags: tuple[str, ...]
    updated_at: datetime | None
    is_discard: bool
    is_deleted: bool


@dataclass(frozen=True, slots=True)
class ReadwiseBook:
    """One Readwise book/document with its highlights, parsed from an export page.

    Book-level fields become Commons facets shared by every highlight it carries
    (`title` from `readable_title`, `author`, `category`); `highlights` are the
    per-Memory records.
    """

    readable_title: str
    author: str
    category: str
    highlights: tuple[ReadwiseHighlightRecord, ...]


@dataclass(frozen=True, slots=True)
class ReadwiseSyncReport:
    """The tally of one sync pass: how each highlight resolved."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0


class ReadwiseHighlight[S = Pending](Model[S, "ReadwiseHighlight[Fetched]"]):
    """Idempotency mapping: a Readwise highlight id to the Memory it produced.

    Keyed by Readwise's stable integer `highlight_id`. `memory_id` links the
    Commons Memory the highlight was mirrored into; `updated_at` records the
    highlight's upstream `updated_at` at ingest, so a re-export is an edit only
    when it carries a newer value.
    """

    highlight_id: ReadwiseHighlight.Col[int] = Integer(primary_key=True)
    memory_id: ReadwiseHighlight.Col[str] = Text(nullable=False)
    updated_at: ReadwiseHighlight.Col[str] = Text(nullable=False)


class ReadwiseSyncState[S = Pending](Model[S, "ReadwiseSyncState[Fetched]"]):
    """Durable key/value sync state (the export watermark), across restarts."""

    key: ReadwiseSyncState.Col[str] = Text(primary_key=True)
    value: ReadwiseSyncState.Col[str] = Text(nullable=False)


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _parse_bool(raw: object) -> bool:
    """Coerce a JSON truthy field (`true`/absent) to a bool, defaulting False."""
    return raw is True


def _parse_datetime(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing `Z`, else None."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_tags(raw: object) -> tuple[str, ...]:
    """Extract `[{name: ...}]` tag names, dropping blanks, order preserved."""
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
    """Parse one highlight mapping, dropping any without a usable integer id."""
    highlight_id = raw.get("id")
    if not isinstance(highlight_id, int):
        return None
    text = raw.get("text")
    note = raw.get("note")
    return ReadwiseHighlightRecord(
        highlight_id=highlight_id,
        text=text if isinstance(text, str) else "",
        note=note.strip() if isinstance(note, str) else "",
        tags=_parse_tags(raw.get("tags")),
        updated_at=_parse_datetime(raw.get("updated_at")),
        is_discard=_parse_bool(raw.get("is_discard")),
        is_deleted=_parse_bool(raw.get("is_deleted")),
    )


def _parse_book(raw: Mapping[str, object]) -> ReadwiseBook:
    """Parse one book mapping, keeping the facet fields and valid highlights."""
    highlights_raw = raw.get("highlights")
    highlights: list[ReadwiseHighlightRecord] = []
    if isinstance(highlights_raw, list):
        for entry in cast("list[object]", highlights_raw):
            if isinstance(entry, Mapping):
                parsed = _parse_highlight(cast("Mapping[str, object]", entry))
                if parsed is not None:
                    highlights.append(parsed)
    return ReadwiseBook(
        readable_title=_string_field(raw, "readable_title"),
        author=_string_field(raw, "author"),
        category=_string_field(raw, "category"),
        highlights=tuple(highlights),
    )


def _string_field(raw: Mapping[str, object], key: str) -> str:
    """Read a string field, trimming it, or `''` when absent/null/non-string."""
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _highlight_content(highlight: ReadwiseHighlightRecord) -> str:
    """The Memory content for a highlight: its text plus a trailing note paragraph.

    The note, when present, is appended as its own `Note: …` paragraph so the
    annotation travels with the highlight it belongs to.
    """
    if highlight.note:
        return f"{highlight.text}\n\nNote: {highlight.note}"
    return highlight.text


def _highlight_facets(
    book: ReadwiseBook, highlight: ReadwiseHighlightRecord
) -> dict[str, str]:
    """The Commons facet set for a highlight: `source` plus the non-empty book and
    tag facets.

    `source: readwise` is always present; `title`/`author`/`category` and a
    comma-joined `tags` are included only when non-empty, keeping the facet set
    free of blank keys.
    """
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


class ReadwiseClient:
    """Cursor pagination and the token check over a `ReadwiseTransport`.

    Walks every export page via `nextPageCursor`, honoring a `429`'s
    `Retry-After` by sleeping and retrying the same cursor, and parses each
    payload into `ReadwiseBook`s. All HTTP lives behind the injected transport,
    so tests drive the client with a scripted fake.

    >>> client = ReadwiseClient(transport=transport)
    >>> await client.verify_token(logger=logger)
    True
    """

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

    async def verify_token(self, *, logger: Logger) -> bool:
        """True when the auth endpoint returns 204 for the configured token."""
        response = await self.transport.verify_token()
        valid = response.status_code == _AUTH_OK_STATUS
        if not valid:
            _info(
                logger,
                "Readwise token check failed",
                status_code=response.status_code,
            )
        return valid

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        include_deleted: bool,
        logger: Logger,
    ) -> list[ReadwiseBook]:
        """Pull every export page into a flat list of parsed books.

        First sync passes `updated_after=None` (a full backfill); an incremental
        sync passes the persisted watermark and `include_deleted=True` so edits
        and soft-deletes are caught. A `429` is retried on its `Retry-After`
        hint up to `max_retries` before the pass gives up.
        """
        books: list[ReadwiseBook] = []
        page_cursor: str | None = None
        while True:
            response = await self._fetch_page(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
                logger=logger,
            )
            payload = response.payload
            results = payload.get("results")
            if isinstance(results, list):
                books.extend(
                    _parse_book(cast("Mapping[str, object]", entry))
                    for entry in cast("list[object]", results)
                    if isinstance(entry, Mapping)
                )
            next_cursor = payload.get("nextPageCursor")
            page_cursor = next_cursor if isinstance(next_cursor, str) else None
            if not page_cursor:
                break
        return books

    async def _fetch_page(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
        logger: Logger,
    ) -> ReadwiseResponse:
        """Fetch one page, retrying a rate-limited response on its `Retry-After`."""
        for _ in range(self.max_retries):
            response = await self.transport.fetch_export(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
            )
            if response.status_code != _RATE_LIMITED_STATUS:
                return response
            delay = (
                response.retry_after.total_seconds()
                if response.retry_after is not None
                else 1.0
            )
            _info(logger, "Readwise rate limited; backing off", delay_seconds=delay)
            await self.sleep(delay)
        message = "Readwise export exhausted rate-limit retries"
        raise ReadwiseAuthError(message)


class ReadwiseSyncService:
    """Reconciler-shaped Readwise ingestion worker over `MemoryService`.

    An idempotent `sync` pass (run at boot and on a periodic loop) pulls the
    export — a full backfill first, then `updatedAfter` + `includeDeleted`
    increments — and folds each highlight against the `readwise_highlight`
    mapping into a create (`capture_tethered`), an edit (`edit_content`), or a
    delete. All writes go through `MemoryService`, so `InvalidateEvent`
    publication and KB/search projection come free. The `updatedAfter` watermark
    (taken from sync start) is persisted only after a fully successful pass, so a
    mid-pass failure re-pulls rather than skipping highlights.
    """

    def __init__(
        self,
        database: Database,
        client: ReadwiseClient,
        memory_service: MemoryService,
    ) -> None:
        self.database: Database = database
        self.client: ReadwiseClient = client
        self.memory_service: MemoryService = memory_service

    async def sync(self, *, logger: Logger) -> ReadwiseSyncReport:
        """Run one idempotent pass; persist the watermark only if it completes.

        The pass start time is captured before the export so an edit landing
        mid-pass is caught by the next incremental sync rather than skipped.
        """
        started_at = datetime.now(UTC)
        watermark = await self._read_watermark()
        include_deleted = watermark is not None
        _debug(
            logger,
            "Readwise sync starting",
            incremental=include_deleted,
            updated_after=watermark.isoformat() if watermark is not None else None,
        )
        books = await self.client.fetch_export(
            updated_after=watermark, include_deleted=include_deleted, logger=logger
        )
        created = updated = deleted = skipped = 0
        for book in books:
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
        await self._store_watermark(started_at)
        _info(
            logger,
            "Readwise sync completed",
            created=created,
            updated=updated,
            deleted=deleted,
            skipped=skipped,
        )
        return ReadwiseSyncReport(
            created=created, updated=updated, deleted=deleted, skipped=skipped
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sync passes on the given interval until cancelled.

        Mirrors the other ingestion workers: a failed pass is logged with its
        traceback and the loop survives, so a transient Readwise outage does not
        take the worker down.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Readwise sync pass failed")

    async def _apply_highlight(
        self,
        book: ReadwiseBook,
        highlight: ReadwiseHighlightRecord,
        *,
        logger: Logger,
    ) -> str:
        """Fold one highlight into a create/edit/delete/skip; return which happened.

        A discarded or upstream-deleted highlight removes any Memory it had
        produced (treated identically). Otherwise a first sighting is captured
        tethered, an unchanged re-export is skipped, and a newer `updated_at`
        edits the Memory content and facets in place.
        """
        mapping = await self._fetch_mapping(highlight.highlight_id)
        if highlight.is_deleted or highlight.is_discard:
            if mapping is None:
                return "skipped"
            await self._delete_highlight(mapping, logger=logger)
            return "deleted"
        return await self._upsert_highlight(book, highlight, mapping, logger=logger)

    async def _upsert_highlight(
        self,
        book: ReadwiseBook,
        highlight: ReadwiseHighlightRecord,
        mapping: ReadwiseHighlight[Fetched] | None,
        *,
        logger: Logger,
    ) -> str:
        """Create or edit the Memory for a live highlight; return which happened.

        A first sighting is captured tethered, an unchanged re-export is skipped,
        and a newer `updated_at` edits the Memory in place. A mapped Memory that
        has vanished out of band is re-mirrored so the highlight stays present.
        """
        content = _highlight_content(highlight)
        if not content.strip():
            return "skipped"
        facets = _highlight_facets(book, highlight)
        if mapping is None:
            await self._create_highlight(highlight, content, facets, logger=logger)
            return "created"
        if not self._is_newer(highlight, mapping):
            return "skipped"
        edited = await self._edit_highlight(
            mapping, highlight, content, facets, logger=logger
        )
        if not edited:
            await self._create_highlight(highlight, content, facets, logger=logger)
            return "created"
        return "updated"

    async def _create_highlight(
        self,
        highlight: ReadwiseHighlightRecord,
        content: str,
        facets: dict[str, str],
        *,
        logger: Logger,
    ) -> None:
        """Capture a highlight as a tethered Memory and record its mapping."""
        memory = await self.memory_service.capture_tethered(
            content,
            provenance=MemoryProvenance(kind="readwise"),
            facets=facets,
            logger=logger,
        )
        await self._store_mapping(highlight, memory.id)

    async def _edit_highlight(
        self,
        mapping: ReadwiseHighlight[Fetched],
        highlight: ReadwiseHighlightRecord,
        content: str,
        facets: dict[str, str],
        *,
        logger: Logger,
    ) -> bool:
        """Edit the mapped Memory in place; False when it no longer exists."""
        memory = await self._fetch_memory(mapping.memory_id)
        if memory is None:
            return False
        try:
            _ = await self.memory_service.edit_content(
                memory, content, facets=facets, logger=logger
            )
        except MemoryConflictError:
            # A concurrent edit bumped the version; the next pass reconciles.
            _info(
                logger,
                "Readwise highlight edit conflicted; deferring",
                highlight_id=mapping.highlight_id,
            )
            return True
        await self._touch_mapping(mapping.highlight_id, highlight.updated_at)
        return True

    async def _delete_highlight(
        self, mapping: ReadwiseHighlight[Fetched], *, logger: Logger
    ) -> None:
        """Soft-delete the mapped Memory and drop the mapping row."""
        memory = await self._fetch_memory(mapping.memory_id)
        if memory is not None:
            # A concurrent out-of-band delete is fine; dropping the mapping below
            # is all that remains to do.
            with contextlib.suppress(MemoryConflictError):
                _ = await self.memory_service.delete(memory, logger=logger)
        await self._remove_mapping(mapping.highlight_id)

    @staticmethod
    def _is_newer(
        highlight: ReadwiseHighlightRecord, mapping: ReadwiseHighlight[Fetched]
    ) -> bool:
        """True when the highlight's `updated_at` is newer than the mapped one.

        An unparseable stored timestamp is treated as stale (edit wins) so a
        highlight is never wedged un-updatable by bad historical data.
        """
        if highlight.updated_at is None:
            return False
        stored = _parse_datetime(mapping.updated_at)
        if stored is None:
            return True
        return highlight.updated_at > stored

    async def _fetch_memory(self, memory_id: str) -> Memory[Fetched] | None:
        """Read the live Memory a mapping points at, or None if gone/deleted."""
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(Memory).where(
                    Memory.id.eq(UUID(memory_id)), Memory.deleted_at.is_null()
                )
            )

    async def _fetch_mapping(
        self, highlight_id: int
    ) -> ReadwiseHighlight[Fetched] | None:
        """Read the mapping row for a Readwise highlight id, if one exists."""
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(ReadwiseHighlight).where(
                    ReadwiseHighlight.highlight_id.eq(highlight_id)
                )
            )

    async def _store_mapping(
        self, highlight: ReadwiseHighlightRecord, memory_id: UUID
    ) -> None:
        """Insert the highlight-to-Memory mapping for a freshly created Memory."""

        async def _insert(tx: Transaction) -> None:
            _ = await tx.execute(
                insert(
                    ReadwiseHighlight(
                        highlight_id=highlight.highlight_id,
                        memory_id=str(memory_id),
                        updated_at=_isoformat_or_empty(highlight.updated_at),
                    )
                )
            )

        await run_in_transaction(self.database, _insert)

    async def _touch_mapping(
        self, highlight_id: int, updated_at: datetime | None
    ) -> None:
        """Record the highlight's new `updated_at` on its mapping after an edit.

        Stored verbatim so a later re-export at the same upstream `updated_at`
        reads as unchanged and is skipped — the same value the create path
        records, keeping the edit-detection comparison consistent.
        """

        async def _update(tx: Transaction) -> None:
            _ = await tx.execute(
                update(ReadwiseHighlight)
                .set(ReadwiseHighlight.updated_at.to(_isoformat_or_empty(updated_at)))
                .where(ReadwiseHighlight.highlight_id.eq(highlight_id))
            )

        await run_in_transaction(self.database, _update)

    async def _remove_mapping(self, highlight_id: int) -> None:
        """Drop a mapping row once its Memory has been deleted."""

        async def _delete(tx: Transaction) -> None:
            connection = tx.require_connection()
            cursor = await connection.execute(
                'DELETE FROM "readwise_highlight" WHERE "highlight_id" = ?',
                (highlight_id,),
            )
            await cursor.close()

        await run_in_transaction(self.database, _delete)

    async def _read_watermark(self) -> datetime | None:
        """The last fully successful `updatedAfter` cursor, or None on first sync."""
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(ReadwiseSyncState).where(
                    ReadwiseSyncState.key.eq(_WATERMARK_KEY)
                )
            )
        return _parse_datetime(row.value) if row is not None else None

    async def _store_watermark(self, watermark: datetime) -> None:
        """Persist the watermark, upserting the single sync-state row."""

        async def _set(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(ReadwiseSyncState).where(
                    ReadwiseSyncState.key.eq(_WATERMARK_KEY)
                )
            )
            if existing is None:
                _ = await tx.execute(
                    insert(
                        ReadwiseSyncState(
                            key=_WATERMARK_KEY, value=watermark.isoformat()
                        )
                    )
                )
            else:
                _ = await tx.execute(
                    update(ReadwiseSyncState)
                    .set(ReadwiseSyncState.value.to(watermark.isoformat()))
                    .where(ReadwiseSyncState.key.eq(_WATERMARK_KEY))
                )

        await run_in_transaction(self.database, _set)


def _isoformat_or_empty(when: datetime | None) -> str:
    """ISO-format a timestamp, or `''` when it is absent."""
    return when.isoformat() if when is not None else ""


@dataclass(frozen=True, slots=True)
class ReaderDocument:
    """One Reader v3 document, parsed from a list-endpoint page.

    `document_id` is Reader's stable string id — the rider keys the shared
    `ebook_document`/`ebook_progress_event` tables under `reader:<document_id>`.
    `reading_progress` is a `0.0`-`1.0` fraction and `location` is one of
    `new`/`later`/`shortlist`/`archive`; both drive the append-dedupe and the
    finished derivation. `read_at` is the receipt time stamped on the telemetry
    event (`last_opened_at` preferred, `updated_at` as fallback).
    """

    document_id: str
    title: str
    author: str
    category: str
    reading_progress: float
    location: str
    read_at: datetime | None


class ReaderTransport(Protocol):
    """The isolated Reader v3 HTTP boundary the `ReaderClient` drives.

    One call, `fetch_list`, pulls a single list page for one category (cursor-
    paginated upstream). Faked in tests so pagination and the progress mapping
    run offline.
    """

    async def fetch_list(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
    ) -> ReadwiseResponse:
        """Fetch one Reader list page for a category (a slice of documents)."""
        ...


def _parse_reader_document(raw: Mapping[str, object]) -> ReaderDocument | None:
    """Parse one Reader document, dropping any without a usable string id."""
    document_id = raw.get("id")
    if not isinstance(document_id, str) or not document_id:
        return None
    reading_progress = raw.get("reading_progress")
    fraction = (
        float(reading_progress) if isinstance(reading_progress, int | float) else 0.0
    )
    return ReaderDocument(
        document_id=document_id,
        title=_string_field(raw, "title"),
        author=_string_field(raw, "author"),
        category=_string_field(raw, "category"),
        reading_progress=fraction,
        location=_string_field(raw, "location"),
        read_at=_parse_datetime(raw.get("last_opened_at"))
        or _parse_datetime(raw.get("updated_at")),
    )


@dataclass(frozen=True, slots=True)
class ReaderSyncReport:
    """The tally of one Reader rider pass: how each document resolved."""

    appended: int = 0
    skipped: int = 0
    finished: int = 0


class ReaderClient:
    """Cursor pagination over a `ReaderTransport`, once per polled category.

    Walks every list page via `nextPageCursor` for each of the polled
    categories, honoring a `429`'s `Retry-After` by sleeping and retrying the
    same cursor, and parses each payload into `ReaderDocument`s. All HTTP lives
    behind the injected transport, so tests drive it with a scripted fake.

    >>> client = ReaderClient(transport=transport)
    >>> documents = await client.fetch_documents(updated_after=None, logger=logger)
    >>> documents[0].category
    'epub'
    """

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

    async def fetch_documents(
        self, *, updated_after: datetime | None, logger: Logger
    ) -> list[ReaderDocument]:
        """Pull every epub/pdf document across all pages into a flat list.

        First sync passes `updated_after=None` (a full backfill); an incremental
        sync passes the persisted watermark. Each category is polled separately
        because the list endpoint accepts a single `category` value per request.
        """
        documents: list[ReaderDocument] = []
        for category in _READER_CATEGORIES:
            page_cursor: str | None = None
            while True:
                response = await self._fetch_page(
                    updated_after=updated_after,
                    category=category,
                    page_cursor=page_cursor,
                    logger=logger,
                )
                results = response.payload.get("results")
                if isinstance(results, list):
                    documents.extend(
                        document
                        for entry in cast("list[object]", results)
                        if isinstance(entry, Mapping)
                        and (
                            document := _parse_reader_document(
                                cast("Mapping[str, object]", entry)
                            )
                        )
                        is not None
                    )
                next_cursor = response.payload.get("nextPageCursor")
                page_cursor = next_cursor if isinstance(next_cursor, str) else None
                if not page_cursor:
                    break
        return documents

    async def _fetch_page(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
        logger: Logger,
    ) -> ReadwiseResponse:
        """Fetch one list page, retrying a rate-limited response on `Retry-After`."""
        for _ in range(self.max_retries):
            response = await self.transport.fetch_list(
                updated_after=updated_after,
                category=category,
                page_cursor=page_cursor,
            )
            if response.status_code != _RATE_LIMITED_STATUS:
                return response
            delay = (
                response.retry_after.total_seconds()
                if response.retry_after is not None
                else 1.0
            )
            _info(logger, "Reader rate limited; backing off", delay_seconds=delay)
            await self.sleep(delay)
        message = "Reader list exhausted rate-limit retries"
        raise ReadwiseAuthError(message)


class ReaderSyncService:
    """The Reader v3 progress rider over the shared ebook Telemetry tables.

    A scheduled poll of the Reader v3 list API folds each epub/pdf document into
    the same `ebook_document`/`ebook_progress_event` tables the kosync gate owns,
    keyed `reader:<id>`. Each pass appends a progress event only when the
    document's `reading_progress` or `location` changed since the last stored
    event (no noise rows from unrelated metadata updates), and the first crossing
    of archive-or-98% mints exactly one machine-synced "Finished reading" Memory
    through `MemoryService.capture_tethered` — once per document, ever. The
    `updatedAfter` watermark (sync start) is persisted only after a fully
    successful pass, so a mid-pass failure re-pulls rather than skipping.
    """

    def __init__(
        self,
        database: Database,
        client: ReaderClient,
        memory_service: MemoryService,
    ) -> None:
        self.database: Database = database
        self.client: ReaderClient = client
        self.memory_service: MemoryService = memory_service

    async def sync(self, *, logger: Logger) -> ReaderSyncReport:
        """Run one idempotent pass; persist the watermark only if it completes.

        The pass start time is captured before the pull so a progress change
        landing mid-pass is caught by the next incremental sync rather than
        skipped.
        """
        started_at = datetime.now(UTC)
        watermark = await self._read_watermark()
        _debug(
            logger,
            "Reader sync starting",
            incremental=watermark is not None,
            updated_after=watermark.isoformat() if watermark is not None else None,
        )
        documents = await self.client.fetch_documents(
            updated_after=watermark, logger=logger
        )
        appended = skipped = finished = 0
        for document in documents:
            outcome = await self._apply_document(document, logger=logger)
            if outcome == "appended":
                appended += 1
            elif outcome == "finished":
                finished += 1
            else:
                skipped += 1
        await self._store_watermark(started_at)
        _info(
            logger,
            "Reader sync completed",
            appended=appended,
            skipped=skipped,
            finished=finished,
        )
        return ReaderSyncReport(appended=appended, skipped=skipped, finished=finished)

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sync passes on the given interval until cancelled.

        Mirrors the other ingestion workers: a failed pass logs its traceback and
        the loop survives, so a transient Reader outage does not take it down.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reader sync pass failed")

    async def _apply_document(self, document: ReaderDocument, *, logger: Logger) -> str:
        """Fold one document into an append/skip, deriving a finished Memory once.

        The document row is upserted first (title refreshed from the API), so the
        finished-once guard reads its prior `finished_captured_at`. A finished
        crossing captures even when the progress row itself was a duplicate.
        """
        key = f"reader:{document.document_id}"
        stored = await self._upsert_document(key, document.title)
        appended = await self._append_if_changed(key, document)
        if self._is_finished(document) and stored.finished_captured_at is None:
            await self._capture_finished(key, document, logger=logger)
            await self._stamp_finished(key)
            return "finished"
        return "appended" if appended else "skipped"

    @staticmethod
    def _is_finished(document: ReaderDocument) -> bool:
        """True when a document is archived or read past the finished threshold."""
        return (
            document.location == _READER_ARCHIVE_LOCATION
            or document.reading_progress >= _READER_FINISHED_THRESHOLD
        )

    async def _upsert_document(self, key: str, title: str) -> EbookDocument[Fetched]:
        """Insert the document on first sighting else refresh its title.

        Returns the row as it stood *before* this pass, so the caller's
        finished-once guard reads the prior `finished_captured_at`.
        """

        async def _upsert(tx: Transaction) -> EbookDocument[Fetched]:
            existing = await tx.fetch_one_or_none(
                select(EbookDocument).where(EbookDocument.document_hash.eq(key))
            )
            if existing is None:
                return await tx.execute(
                    insert(
                        EbookDocument(document_hash=key, title=title or None)
                    ).returning()
                )
            _ = await tx.execute(
                update(EbookDocument)
                .set(
                    EbookDocument.title.to(title or None),
                    EbookDocument.updated_at.to(CurrentTimestamp),
                )
                .where(EbookDocument.document_hash.eq(key))
            )
            return existing

        return await run_in_transaction(self.database, _upsert)

    async def _append_if_changed(self, key: str, document: ReaderDocument) -> bool:
        """Append a progress event unless it repeats the latest stored one.

        Reader emits document updates for many metadata reasons; only a changed
        `reading_progress` or `location` is reading movement worth a telemetry
        row, so an otherwise-identical latest event is left as the head.
        """
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
        """The newest stored progress event for a document key, if any."""
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(EbookProgressEvent)
                .where(EbookProgressEvent.document_hash.eq(key))
                .order_by(EbookProgressEvent.id.desc())
                .limit(1)
            )

    async def _append_event(self, key: str, document: ReaderDocument) -> None:
        """Append one immutable progress event for a Reader document."""
        read_at = document.read_at or datetime.now(UTC)

        async def _insert(tx: Transaction) -> None:
            _ = await tx.execute(
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

        await run_in_transaction(self.database, _insert)

    async def _capture_finished(
        self, key: str, document: ReaderDocument, *, logger: Logger
    ) -> None:
        """Mint the one machine-synced "finished reading" Memory for a document.

        Content and the `title` facet name the book when the API gave a title; an
        untitled document falls back to its key so the capture still happens. The
        `author` facet is added only when present.
        """
        content = (
            f"Finished reading {document.title}"
            if document.title
            else f"{key} (unlabeled ebook)"
        )
        facets = {"source": _READER_DEVICE, "category": "ebook"}
        if document.title:
            facets["title"] = document.title
        if document.author:
            facets["author"] = document.author
        _ = await self.memory_service.capture_tethered(
            content,
            provenance=MemoryProvenance(kind="readwise"),
            facets=facets,
            logger=logger,
        )

    async def _stamp_finished(self, key: str) -> None:
        """Record that the finished Memory for a document has been minted."""

        async def _stamp(tx: Transaction) -> None:
            _ = await tx.execute(
                update(EbookDocument)
                .set(EbookDocument.finished_captured_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(key))
            )

        await run_in_transaction(self.database, _stamp)

    async def _read_watermark(self) -> datetime | None:
        """The last fully successful `updatedAfter` cursor, or None on first sync."""
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(ReadwiseSyncState).where(
                    ReadwiseSyncState.key.eq(_READER_WATERMARK_KEY)
                )
            )
        return _parse_datetime(row.value) if row is not None else None

    async def _store_watermark(self, watermark: datetime) -> None:
        """Persist the Reader watermark, upserting its single sync-state row."""

        async def _set(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(ReadwiseSyncState).where(
                    ReadwiseSyncState.key.eq(_READER_WATERMARK_KEY)
                )
            )
            if existing is None:
                _ = await tx.execute(
                    insert(
                        ReadwiseSyncState(
                            key=_READER_WATERMARK_KEY, value=watermark.isoformat()
                        )
                    )
                )
            else:
                _ = await tx.execute(
                    update(ReadwiseSyncState)
                    .set(ReadwiseSyncState.value.to(watermark.isoformat()))
                    .where(ReadwiseSyncState.key.eq(_READER_WATERMARK_KEY))
                )

        await run_in_transaction(self.database, _set)


class HttpReaderTransport(ReaderTransport):
    """The production `ReaderTransport`: a thin httpx client over the v3 list API.

    Holds the same API key the v2 gate uses and performs one GET per list page,
    normalizing it into a `ReadwiseResponse`. Pagination and progress semantics
    live above it in `ReaderClient`/`ReaderSyncService`, keeping this boundary
    dumb and faked-in-tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: timedelta | None = None,
    ) -> None:
        if not api_key:
            message = "Readwise API key is required to build the Reader transport"
            raise ReadwiseConfigurationError(message)
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._timeout: timedelta = timeout or timedelta(seconds=30)

    async def fetch_list(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
    ) -> ReadwiseResponse:
        params: dict[str, str] = {
            "category": category,
            "limit": str(_READER_LIMIT),
        }
        if updated_after is not None:
            params["updatedAfter"] = updated_after.isoformat()
        if page_cursor is not None:
            params["pageCursor"] = page_cursor
        async with httpx2.AsyncClient(
            base_url=self._base_url, timeout=self._timeout.total_seconds()
        ) as client:
            response = await client.get(
                _LIST_PATH,
                params=params,
                headers={"Authorization": f"Token {self._api_key}"},
            )
        return _from_httpx(response)


class HttpReadwiseTransport(ReadwiseTransport):
    """The production `ReadwiseTransport`: a thin httpx client over the v2 API.

    Holds the API key and base URL and performs the two GETs (export, auth),
    normalizing each into a `ReadwiseResponse`. All pagination and highlight
    semantics live above it in `ReadwiseClient`/`ReadwiseSyncService`, keeping
    this boundary dumb and faked-in-tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: timedelta | None = None,
    ) -> None:
        if not api_key:
            message = "Readwise API key is required to build the HTTP transport"
            raise ReadwiseConfigurationError(message)
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._timeout: timedelta = timeout or timedelta(seconds=30)

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> ReadwiseResponse:
        params: dict[str, str] = {}
        if updated_after is not None:
            params["updatedAfter"] = updated_after.isoformat()
        if include_deleted:
            params["includeDeleted"] = "true"
        if page_cursor is not None:
            params["pageCursor"] = page_cursor
        return await self._get(_EXPORT_PATH, params=params)

    async def verify_token(self) -> ReadwiseResponse:
        return await self._get(_AUTH_PATH)

    async def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> ReadwiseResponse:
        async with httpx2.AsyncClient(
            base_url=self._base_url, timeout=self._timeout.total_seconds()
        ) as client:
            response = await client.get(
                path,
                params=dict(params or {}),
                headers={"Authorization": f"Token {self._api_key}"},
            )
        return _from_httpx(response)


def _from_httpx(response: Any) -> ReadwiseResponse:
    """Normalize an httpx response into a `ReadwiseResponse` (decode JSON best-effort)."""
    try:
        body = response.json()
    except Exception:
        body = {}
    payload: Mapping[str, object] = (
        cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}
    )
    return ReadwiseResponse(
        status_code=int(response.status_code),
        payload=payload,
        retry_after=_retry_after_seconds(response.headers),
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` header into a timedelta, if present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    return None


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
}


async def create_readwise_schema(database: Database) -> None:
    """Bring the Readwise ingestion schema to current on an initialized database.

    Applies the frozen migration chain: the highlight-to-Memory mapping table and
    the sync-state key/value store. The caller owns `Database.initialize` and
    hands the live database here before serving requests.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_readwise_schema(database)
    """
    await database.migrate(_READWISE_MIGRATIONS)


__all__ = [
    "HttpReaderTransport",
    "HttpReadwiseTransport",
    "ReaderClient",
    "ReaderDocument",
    "ReaderSyncReport",
    "ReaderSyncService",
    "ReaderTransport",
    "ReadwiseAuthError",
    "ReadwiseBook",
    "ReadwiseClient",
    "ReadwiseConfigurationError",
    "ReadwiseHighlight",
    "ReadwiseHighlightRecord",
    "ReadwiseResponse",
    "ReadwiseSyncReport",
    "ReadwiseSyncService",
    "ReadwiseSyncState",
    "ReadwiseTransport",
    "create_readwise_schema",
]
