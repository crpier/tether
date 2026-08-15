"""Converge the derived YouTube corpus Search index with canonical SQLite rows.

Each active video's title, description, and fetched transcript form its searchable
text. The LanceDB projection is disposable and rebuildable; this module is its
sole writer. Chunks and vectors are re-derived on demand. Deterministic,
model-stamped chunk ids let each pass embed only absent content and remove ids no
active video still produces.

That single `list_ids()` diff covers every case with no extra bookkeeping:

- *cold / wiped index* — nothing is present, so every chunk is embedded;
- *new or edited transcript* — changed text yields new content-stamped ids that
  embed, while superseded ids fall out as orphans;
- *ignored / deleted video* — its chunks are no longer desired and are removed;
- *model swap* — the id folds in the model name + width, so every id changes:
  the corpus re-embeds under the new model and the old ids are dropped.

The reconciler talks to the index only through `YouTubeSearchIndexPort` and to the
model only through `Embedder`, so it is fully testable against fakes of both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from snekql.sqlite import select

from tether.reconcile_loop import run_reconcile_loop
from tether.youtube import IngestedVideo
from tether.youtube_search_chunks import chunk_youtube_text
from tether.youtube_search_index import ChunkDocument, chunk_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from snekql.sqlite import Database

    from tether.embeddings import Embedder
    from tether.structured_logging import Logger

# Transformer inference memory grows steeply with batch size because every
# document is padded to the longest sequence. Keep cold corpus rebuilds within
# the memory budget of small self-hosted machines.
_DEFAULT_EMBED_BATCH = 8


class YouTubeSearchIndexPort(Protocol):
    """The slice of `YouTubeSearchIndex` the reconciler writes through."""

    async def upsert(self, documents: Sequence[ChunkDocument]) -> None: ...
    async def remove(self, ids: Sequence[UUID]) -> None: ...
    async def list_ids(self) -> set[UUID]: ...
    async def optimize(self, *, logger: Logger) -> None: ...


@dataclass(frozen=True, slots=True)
class _ChunkSpec:
    """A desired chunk before embedding: its id, parent video, and text."""

    id: UUID
    video_id: str
    content: str


@dataclass(frozen=True, slots=True)
class YouTubeSearchReconcileReport:
    """What a reconcile pass did, for logging and tests."""

    indexed: int
    """Chunks in the desired set (every active transcript's chunks)."""
    embedded: int
    """Chunks embedded this pass (those absent from the index)."""
    removed: int
    """Orphan chunk ids dropped (no live transcript produces them)."""


class YouTubeSearchReconciler:
    """Converge the YouTube text-chunk projection with canonical SQLite rows."""

    def __init__(
        self,
        *,
        database: Database,
        index: YouTubeSearchIndexPort,
        embedder: Embedder,
        chunk_max_chars: int = 2000,
        chunk_overlap_chars: int = 200,
    ) -> None:
        self.database: Database = database
        self.index: YouTubeSearchIndexPort = index
        self.embedder: Embedder = embedder
        self.chunk_max_chars: int = chunk_max_chars
        self.chunk_overlap_chars: int = chunk_overlap_chars
        # Embed batch size is a fixed module constant rather than a constructor
        # knob; it bounds request size and never needs per-instance tuning.
        self.embed_batch_size: int = _DEFAULT_EMBED_BATCH

    async def reconcile(self, *, logger: Logger) -> YouTubeSearchReconcileReport:
        """Bring the chunk index in step with active saved videos; idempotent."""
        specs = await self._desired_chunks()
        desired_ids = {spec.id for spec in specs}
        present = await self.index.list_ids()

        owed = [spec for spec in specs if spec.id not in present]
        await self._embed_and_upsert(owed)

        orphans = [
            identifier for identifier in present if identifier not in desired_ids
        ]
        if orphans:
            await self.index.remove(orphans)
        await self.index.optimize(logger=logger)

        report = YouTubeSearchReconcileReport(
            indexed=len(desired_ids),
            embedded=len(owed),
            removed=len(orphans),
        )
        logger.info(
            "YouTube search index reconciled",
            indexed=report.indexed,
            embedded=report.embedded,
            removed=report.removed,
        )
        return report

    async def reconcile_forever(
        self,
        *,
        interval_seconds: float,
        logger: Logger,
        initial_delay_seconds: float = 5.0,
    ) -> None:
        """Run `reconcile` on a fixed interval until cancelled.

        The YouTube index has no boot reconcile (a cold pass re-embeds the
        whole corpus and would block startup), so this loop is what fills and
        maintains it — the first pass runs shortly after boot (`initial_delay_
        seconds`, not a full interval) so transcripts become searchable quickly.
        The short delay also keeps shutdown clean: a host that stops moments after
        boot cancels this task while it is still sleeping, never mid-pass. A failed
        pass is logged and swallowed so a transient error never kills the loop;
        the next tick retries."""
        await run_reconcile_loop(
            lambda: self.reconcile(logger=logger),
            interval_seconds=interval_seconds,
            initial_delay_seconds=initial_delay_seconds,
            logger=logger,
            failure_message="Periodic YouTube search reconcile failed; retrying next tick",
        )

    async def _desired_chunks(self) -> list[_ChunkSpec]:
        """Re-derive the desired chunk set from every active video.

        The indexed text leads with the title and description so a video stays
        searchable by either even before (or without) a transcript — matching the
        surface the old keyword search covered — and the transcript follows. A
        video with no searchable text at all yields no chunks and is skipped."""
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(
                select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
            )
        specs: list[_ChunkSpec] = []
        for video in videos:
            source = "\n".join(
                part
                for part in (video.title, video.description, video.transcript)
                if part
            )
            chunks = chunk_youtube_text(
                source,
                max_chars=self.chunk_max_chars,
                overlap_chars=self.chunk_overlap_chars,
            )
            for index, content in enumerate(chunks):
                specs.append(
                    _ChunkSpec(
                        id=chunk_id(
                            model=self.embedder.model_name,
                            vector_dim=self.embedder.vector_dim,
                            video_id=video.video_id,
                            index=index,
                            content=content,
                        ),
                        video_id=video.video_id,
                        content=content,
                    )
                )
        return specs

    async def _embed_and_upsert(self, owed: Sequence[_ChunkSpec]) -> None:
        """Embed owed chunks in bounded batches and upsert them."""
        for start in range(0, len(owed), self.embed_batch_size):
            batch = owed[start : start + self.embed_batch_size]
            vectors = await self.embedder.embed_documents(
                [spec.content for spec in batch]
            )
            await self.index.upsert(
                [
                    ChunkDocument(
                        id=spec.id,
                        video_id=spec.video_id,
                        content=spec.content,
                        vector=vector,
                    )
                    for spec, vector in zip(batch, vectors, strict=True)
                ]
            )
