"""KOReader kosync ingestion gate: Tether serves the kosync sync protocol.

Tether's host *is* the kosync server. KOReader devices push per-document reading
progress straight at it — no third party, fully self-hosted. The module owns
three concerns:

- **Telemetry storage.** Every progress push appends one `ebook_progress_event`
  row (server clock), and upserts a per-document `ebook_document`. Progress
  events are raw time-series Telemetry: they live in these vertical tables and
  never enter the Memory pool as-is.
- **Finished derivation.** The first push that carries a document past
  `FINISHED_THRESHOLD` mints exactly one machine-synced Memory ("Finished
  reading <title>"), trusted at capture through `MemoryService.capture_tethered`
  — the only thing that crosses from Telemetry into the Commons, and only once
  per document ever.
- **Hash→title mapping.** KOReader identifies a document by a hash, not a title.
  Devices must set KOReader's document-matching method to **filename** (Settings
  -> Document -> Sync -> Progress sync -> "Document matching method" =
  *Filename*), which makes the hash `md5(basename)`; the KOReader default is a
  binary partial-MD5 that Tether cannot map back to a title. `label_ebook` and
  `match_ebook_filename` attach a human title so a finished Memory can name the
  book instead of its hash.

>>> service = KosyncService(database=database, memory_service=memories)
>>> timestamp = await service.record_progress(
...     ProgressUpdate(
...         document="abc", percentage=0.5, progress="/body/DocFragment[3]",
...         device="Phone", device_id="",
...     ),
...     logger=logger,
... )
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
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
    Transaction,
    insert,
    select,
    update,
)

from tether.db_retry import run_in_transaction
from tether.logging import Logger
from tether.memories import MemoryProvenance, MemoryService

FINISHED_THRESHOLD = 0.98
"""Reading fraction at or beyond which a document is treated as finished.

KOReader reports `percentage` in `0.0` to `1.0`; the last pages of an epub often
never reach a literal `1.0`, so `0.98` is the pragmatic "done" line. The derived
Memory fires once per document ever (guarded by `finished_captured_at`), so the
threshold only decides *when* the single capture happens, never how many."""


class EbookProgressEvent[S = Pending](Model[S, "EbookProgressEvent[Fetched]"]):
    """One reading-progress push from a device: append-only Telemetry.

    Never mutated and never deleted — the furthest-progress view is simply the
    highest-`id` row for a `document_hash`. `timestamp` is the server clock at
    receipt (seconds), the value echoed back to the device; `received_at` is the
    same instant as an ISO string for human inspection.
    """

    id: EbookProgressEvent.GenCol[int] = Integer(
        primary_key=True, default=PENDING_GENERATION
    )
    document_hash: EbookProgressEvent.Col[str] = Text(nullable=False)
    percentage: EbookProgressEvent.Col[float] = Real(nullable=False)
    progress: EbookProgressEvent.Col[str] = Text(nullable=False)
    device: EbookProgressEvent.Col[str] = Text(nullable=False)
    device_id: EbookProgressEvent.Col[str] = Text(nullable=False)
    timestamp: EbookProgressEvent.Col[int] = Integer(nullable=False)
    received_at: EbookProgressEvent.GenCol[datetime] = Text(default=CurrentTimestamp)
    __indexes__: ClassVar = [Index(document_hash)]


class EbookDocument[S = Pending](Model[S, "EbookDocument[Fetched]"]):
    """A document Tether has seen progress for, keyed by its KOReader hash.

    Upserted on the first push of an unknown hash. `title` is null until the
    agent labels it (`label_ebook`/`match_ebook_filename`); `finished_captured_at`
    is null until the finished Memory has been minted, and being non-null is the
    once-ever guard that stops a re-read re-firing the capture.
    """

    document_hash: EbookDocument.Col[str] = Text(primary_key=True)
    title: EbookDocument.Col[str | None] = Text(default=None, nullable=True)
    finished_captured_at: EbookDocument.Col[str | None] = Text(
        default=None, nullable=True
    )
    created_at: EbookDocument.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: EbookDocument.GenCol[datetime] = Text(default=CurrentTimestamp)


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """A validated progress push, before it is stored.

    `device_id` is optional on the wire and normalised to `''` when absent, so
    the stored row and the echoed reply always carry the field.
    """

    document: str
    percentage: float
    progress: str
    device: str
    device_id: str


@dataclass(frozen=True, slots=True)
class LatestProgress:
    """The furthest-progress view of a document: the newest stored event."""

    document: str
    percentage: float
    progress: str
    device: str
    device_id: str
    timestamp: int


def ebook_hash_for_filename(filename: str) -> str:
    """The KOReader filename-mode document hash: `md5` of the path's basename.

    Mirrors KOReader's "filename" document-matching method, which hashes the
    bare basename (no directory), so a title the agent knows a filename for can
    be mapped onto the hash a device will push under.

    >>> ebook_hash_for_filename("/mnt/onboard/Deep Work.epub")
    '0d2b8f...'  # doctest: +SKIP
    """
    basename = PurePosixPath(filename).name
    return hashlib.md5(basename.encode("utf-8")).hexdigest()  # noqa: S324


class KosyncService:
    """The kosync ingestion gate over the ebook Telemetry tables + Memory.

    Every device push lands as append-only Telemetry (`record_progress`); a
    crossing of `FINISHED_THRESHOLD` mints one machine-synced Memory through
    `MemoryService.capture_tethered`, so the Commons projection and search index
    are written exactly as any tether would. Labeling (`label_ebook`,
    `match_ebook_filename`, `list_unlabeled`) is how a hash acquires a human
    title. All writes go through `run_in_transaction` for retry safety.
    """

    def __init__(
        self,
        database: Database,
        memory_service: MemoryService,
    ) -> None:
        self.database: Database = database
        self.memory_service: MemoryService = memory_service

    async def record_progress(
        self, update: ProgressUpdate, *, logger: Logger, now: datetime
    ) -> int:
        """Store one push as Telemetry and derive a finished Memory if crossed.

        Returns the server timestamp (unix seconds) echoed back to the device.
        The document is upserted first, so its `finished_captured_at` reflects
        prior pushes only — a first-ever push at or past the threshold captures,
        a later one after the guard is stamped does not.
        """
        server_timestamp = int(now.timestamp())
        document = await self._upsert_document(update.document)
        await self._append_event(update, server_timestamp)
        if (
            update.percentage >= FINISHED_THRESHOLD
            and document.finished_captured_at is None
        ):
            await self._capture_finished(document, logger=logger)
            await self._stamp_finished(update.document)
        return server_timestamp

    async def latest_progress(self, document: str) -> LatestProgress | None:
        """The newest stored event for a document, or None when none exists."""
        async with self.database.transaction() as tx:
            event = await tx.fetch_one_or_none(
                select(EbookProgressEvent)
                .where(EbookProgressEvent.document_hash.eq(document))
                .order_by(EbookProgressEvent.id.desc())
                .limit(1)
            )
        if event is None:
            return None
        return LatestProgress(
            document=event.document_hash,
            percentage=event.percentage,
            progress=event.progress,
            device=event.device,
            device_id=event.device_id,
            timestamp=event.timestamp,
        )

    async def label_ebook(
        self, document_hash: str, title: str
    ) -> EbookDocument[Fetched]:
        """Attach a human title to a document, upserting the row if unseen.

        Labeling a hash the device has not pushed yet is allowed: the row is
        created title-first so the eventual finished Memory names the book.
        """

        async def _label(tx: Transaction) -> EbookDocument[Fetched]:
            existing = await tx.fetch_one_or_none(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )
            if existing is None:
                return await tx.execute(
                    insert(
                        EbookDocument(document_hash=document_hash, title=title)
                    ).returning()
                )
            _ = await tx.execute(
                update(EbookDocument)
                .set(
                    EbookDocument.title.to(title),
                    EbookDocument.updated_at.to(CurrentTimestamp),
                )
                .where(EbookDocument.document_hash.eq(document_hash))
            )
            return await tx.fetch_one(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )

        return await run_in_transaction(self.database, _label)

    async def match_ebook_filename(self, filename: str) -> EbookDocument[Fetched]:
        """Label the document a filename hashes to, deriving the title from it.

        Computes the KOReader filename-mode hash and labels that document with
        the filename's stem, so the agent can map a book it knows the filename of
        without the user reciting a raw hash.
        """
        return await self.label_ebook(
            ebook_hash_for_filename(filename), PurePosixPath(filename).stem
        )

    async def list_unlabeled(self) -> list[EbookDocument[Fetched]]:
        """Every document still without a title, oldest first.

        The agent reads this to ask the user which book an unknown hash is, then
        labels it — the hash→title mapping the protocol itself never carries.
        """
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(EbookDocument)
                .where(EbookDocument.title.is_null())
                .order_by(EbookDocument.created_at.asc())
            )

    async def _upsert_document(self, document_hash: str) -> EbookDocument[Fetched]:
        """Insert a document row on first sighting, else touch `updated_at`.

        Returns the row as it stood *before* this push, so the caller's
        finished-once guard reads the prior `finished_captured_at`.
        """

        async def _upsert(tx: Transaction) -> EbookDocument[Fetched]:
            existing = await tx.fetch_one_or_none(
                select(EbookDocument).where(
                    EbookDocument.document_hash.eq(document_hash)
                )
            )
            if existing is None:
                return await tx.execute(
                    insert(EbookDocument(document_hash=document_hash)).returning()
                )
            _ = await tx.execute(
                update(EbookDocument)
                .set(EbookDocument.updated_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(document_hash))
            )
            return existing

        return await run_in_transaction(self.database, _upsert)

    async def _append_event(
        self, update: ProgressUpdate, server_timestamp: int
    ) -> None:
        """Append one immutable progress event with the server's receipt time."""

        async def _insert(tx: Transaction) -> None:
            _ = await tx.execute(
                insert(
                    EbookProgressEvent(
                        document_hash=update.document,
                        percentage=update.percentage,
                        progress=update.progress,
                        device=update.device,
                        device_id=update.device_id,
                        timestamp=server_timestamp,
                    )
                )
            )

        await run_in_transaction(self.database, _insert)

    async def _capture_finished(
        self, document: EbookDocument[Fetched], *, logger: Logger
    ) -> None:
        """Mint the one machine-synced "finished reading" Memory for a document.

        Content and the `title` facet name the book when it is labeled; an
        unlabeled document falls back to its hash so the capture still happens —
        the user can relabel later, but the finished event is never dropped.
        """
        title = document.title
        content = (
            f"Finished reading {title}"
            if title
            else f"{document.document_hash} (unlabeled ebook)"
        )
        facets = {"source": "koreader", "category": "ebook"}
        if title:
            facets["title"] = title
        _ = await self.memory_service.capture_tethered(
            content,
            provenance=MemoryProvenance(kind="koreader"),
            facets=facets,
            logger=logger,
        )

    async def _stamp_finished(self, document_hash: str) -> None:
        """Record that the finished Memory for a document has been minted."""

        async def _stamp(tx: Transaction) -> None:
            _ = await tx.execute(
                update(EbookDocument)
                .set(EbookDocument.finished_captured_at.to(CurrentTimestamp))
                .where(EbookDocument.document_hash.eq(document_hash))
            )

        await run_in_transaction(self.database, _stamp)


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


__all__ = [
    "FINISHED_THRESHOLD",
    "EbookDocument",
    "EbookProgressEvent",
    "KosyncService",
    "LatestProgress",
    "ProgressUpdate",
    "create_kosync_schema",
    "ebook_hash_for_filename",
]
