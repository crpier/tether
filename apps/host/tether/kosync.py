"""KOReader progress synchronization over canonical reading Evidence."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from snekql.sqlite import Fetched

from tether.kosync_model import (
    FINISHED_THRESHOLD,
    LatestProgress,
    ProgressUpdate,
    ebook_hash_for_filename,
)
from tether.kosync_store import EbookDocument, KosyncStore
from tether.structured_logging import Logger


class KosyncService:
    """Persist KOReader progress without deriving Memory directly."""

    def __init__(self, store: KosyncStore) -> None:
        self.store: KosyncStore = store

    async def record_progress(
        self, update: ProgressUpdate, *, logger: Logger, now: datetime
    ) -> int:
        """Append one canonical progress event and mark first completion."""
        _ = logger
        server_timestamp = int(now.timestamp())
        document = await self.store.touch_document(update.document)
        await self.store.append_event(update, server_timestamp)
        if update.percentage >= FINISHED_THRESHOLD and document.finished_at is None:
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
