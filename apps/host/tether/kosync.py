"""KOReader progress synchronization and finished-book orchestration."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol

from snekql.sqlite import Fetched

from tether.kosync_model import (
    FINISHED_THRESHOLD,
    LatestProgress,
    ProgressUpdate,
    ebook_hash_for_filename,
)
from tether.kosync_store import EbookDocument, KosyncStore
from tether.memory_store import MemoryProvenance
from tether.structured_logging import Logger


class FinishedMemoryPort(Protocol):
    """Capture the trusted Memory derived when an ebook is first finished."""

    async def capture_tethered(
        self,
        content: str,
        *,
        provenance: MemoryProvenance,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> object:
        """Capture machine-synced content directly into the tethered corpus."""
        ...


class KosyncService:
    """Coordinate KOReader progress persistence and finished-book derivation.

    Progress writes remain separate from finished-Memory capture. If that
    downstream capture defects, the raw event stays authoritative and the
    unstamped document remains resumable on the next push.

    >>> service = KosyncService(store=store, memory_service=memories)
    >>> timestamp = await service.record_progress(
    ...     ProgressUpdate(
    ...         document="abc",
    ...         percentage=0.5,
    ...         progress="/body/DocFragment[3]",
    ...         device="Phone",
    ...         device_id="",
    ...     ),
    ...     logger=logger,
    ...     now=now,
    ... )
    """

    def __init__(
        self,
        store: KosyncStore,
        memory_service: FinishedMemoryPort,
    ) -> None:
        self.store: KosyncStore = store
        self.memory_service: FinishedMemoryPort = memory_service

    async def record_progress(
        self, update: ProgressUpdate, *, logger: Logger, now: datetime
    ) -> int:
        """Persist one push and derive a finished Memory when first crossed."""
        server_timestamp = int(now.timestamp())
        document = await self.store.touch_document(update.document)
        await self.store.append_event(update, server_timestamp)
        if (
            update.percentage >= FINISHED_THRESHOLD
            and document.finished_captured_at is None
        ):
            await self._capture_finished(document, logger=logger)
            await self.store.stamp_finished(update.document)
        return server_timestamp

    async def latest_progress(self, document: str) -> LatestProgress | None:
        """Return the newest stored event for a document, if present."""
        event = await self.store.fetch_latest_event(document)
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
        """Attach a title to a document, creating its identity when unseen."""
        return await self.store.label_document(document_hash, title)

    async def match_ebook_filename(self, filename: str) -> EbookDocument[Fetched]:
        """Label the filename-mode document hash with the filename stem."""
        return await self.label_ebook(
            ebook_hash_for_filename(filename), PurePosixPath(filename).stem
        )

    async def list_unlabeled(self) -> list[EbookDocument[Fetched]]:
        """Return every document still missing a title, oldest first."""
        return await self.store.list_unlabeled()

    async def _capture_finished(
        self, document: EbookDocument[Fetched], *, logger: Logger
    ) -> None:
        """Mint the once-per-document machine-synced finished Memory."""
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
