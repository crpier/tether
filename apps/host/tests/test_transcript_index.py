"""Tests for the LanceDB transcript-chunk index adapter.

`TranscriptIndex` is a second hybrid retriever alongside `SearchIndex`, shaped
for transcript chunks: every row carries its parent `video_id`, and a hit
returns that id plus the chunk text as a snippet so the search path can dedupe
chunks to videos and show why each matched — without re-fetching the transcript.
These tests drive the real embedded LanceDB in a temp dir with hand-built
vectors, mirroring `test_search_index.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, assert_in, assert_raises, test

from tether.transcript_index import (
    ChunkDocument,
    TranscriptIndex,
    VectorDimMismatchError,
)

if TYPE_CHECKING:
    from tether.logging import Logger

_DIM = 4

# The lance-internal error message optimize() self-heals on: a corrupt fragment
# whose compaction batch-decode trips over an offset that overruns its values
# buffer. Row reads still work, so a rewrite salvages the data losslessly.
_LANCE_CORRUPTION_MESSAGE = (
    "lance error: Encountered internal error. Please file a bug report at "
    "https://github.com/lance-format/lance/issues. Error decoding batch: "
    "LanceError(Arrow): Invalid argument error: Max offset of 5157331 exceeds "
    "length of values 3031848"
)


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.transcript_index")


class _RaiseOnceOptimize:
    """Wraps a real AsyncTable, raising the lance corruption on first optimize().

    Everything else delegates to the wrapped table, so the salvage path can still
    read the rows back out to rebuild from.
    """

    def __init__(self, table: Any) -> None:
        self._table = table
        self.raised = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)

    async def optimize(self, *args: Any, **kwargs: Any) -> Any:
        if not self.raised:
            self.raised = True
            raise RuntimeError(_LANCE_CORRUPTION_MESSAGE)
        return await self._table.optimize(*args, **kwargs)


def _doc(video_id: str, content: str, vector: list[float]) -> ChunkDocument:
    return ChunkDocument(id=uuid4(), video_id=video_id, content=content, vector=vector)


@test()
async def open_is_idempotent_and_persists_across_reopen() -> None:
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        kept = _doc("vid1", "android developer registration", [1.0, 0.0, 0.0, 0.0])
        index = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([kept])
        assert_eq(await index.count(), 1)

        reopened = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)

        assert_eq(await reopened.count(), 1)
        hits = await reopened.search(
            text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_in("vid1", {hit.video_id for hit in hits})


@test()
async def a_hit_carries_its_video_id_and_snippet() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        target = _doc(
            "vid1", "google forced developer signing for android", [1.0, 0.0, 0.0, 0.0]
        )
        other = _doc("vid2", "grocery shopping list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([target, other])

        hits = await index.search(text="signing", vector=[0.0, 0.0, 0.0, 1.0], limit=5)

        match = next(hit for hit in hits if hit.video_id == "vid1")
        assert_eq(match.snippet, "google forced developer signing for android")


@test()
async def multiple_chunks_can_share_one_video_id() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert(
            [
                _doc("vid1", "first chunk about android", [1.0, 0.0, 0.0, 0.0]),
                _doc("vid1", "second chunk about developers", [0.0, 1.0, 0.0, 0.0]),
            ]
        )

        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        assert_eq({hit.video_id for hit in hits}, {"vid1"})


@test()
async def a_quoted_phrase_query_does_not_crash_the_search() -> None:
    """A model-issued phrase query (quoted terms) must resolve, not error.

    LanceDB phrase queries need an FTS index built with token positions; without
    them the search raises `position is not found but required for phrase
    queries`. The index opts into positions so quoted queries are valid.
    """
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert(
            [_doc("vid1", "a terminal agent written in rust", [1.0, 0.0, 0.0, 0.0])]
        )

        hits = await index.search(
            text='"terminal agent"', vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_in("vid1", {hit.video_id for hit in hits})


@test()
async def candidates_carry_a_relevance_score() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        strong = _doc("vid1", "android android developers", [1.0, 0.0, 0.0, 0.0])
        weak = _doc("vid2", "unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([strong, weak])

        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

        by_id = {hit.chunk_id: hit.score for hit in hits}
        assert_gt(by_id[strong.id], 0.0)
        assert_gt(by_id[strong.id], by_id[weak.id])


@test()
async def remove_drops_chunks_by_id() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        gone = _doc("vid1", "android note", [1.0, 0.0, 0.0, 0.0])
        kept = _doc("vid2", "grocery note", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([gone, kept])

        await index.remove([gone.id])

        assert_eq(await index.list_ids(), {kept.id})


@test()
async def list_ids_reports_every_indexed_chunk() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        assert_eq(await index.list_ids(), set())
        first = _doc("vid1", "android note", [1.0, 0.0, 0.0, 0.0])
        second = _doc("vid2", "grocery list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([first, second])

        assert_eq(await index.list_ids(), {first.id, second.id})


@test()
async def rebuild_replaces_the_whole_projection() -> None:
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert([_doc("old", "stale chunk", [1.0, 0.0, 0.0, 0.0])])
        fresh = _doc("new", "fresh chunk", [0.0, 1.0, 0.0, 0.0])

        await index.rebuild([fresh])

        assert_eq(await index.list_ids(), {fresh.id})


@test()
async def optimize_self_heals_a_corrupt_fragment_without_losing_rows() -> None:
    """A lance compaction-decode bug must not wedge the index forever.

    The fragment's rows still read, so optimize() catches the internal error and
    rewrites the table from those rows: no data lost, no re-embedding, and the
    next optimize succeeds on the healthy dataset.
    """
    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        kept = [
            _doc("vid1", "android developer signing", [1.0, 0.0, 0.0, 0.0]),
            _doc("vid2", "grocery shopping list", [0.0, 1.0, 0.0, 0.0]),
        ]
        await index.upsert(kept)
        before = await index.list_ids()
        # Force the next optimize to hit the lance corruption error once.
        # Swap in a table whose first optimize() raises the lance corruption.
        index._table = _RaiseOnceOptimize(index._table)  # pyright: ignore[reportAttributeAccessIssue]

        await index.optimize(logger=_logger())  # self-heals instead of raising

        assert_eq(await index.list_ids(), before)
        # The rewrite left a healthy dataset: a follow-up optimize is clean, and
        # search still resolves against the salvaged rows.
        await index.optimize(logger=_logger())
        hits = await index.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)
        assert_in("vid1", {hit.video_id for hit in hits})


@test()
async def optimize_reraises_errors_that_are_not_lance_corruption() -> None:
    """A non-corruption failure must propagate, not silently rebuild the index."""

    class _RaiseUnrelated:
        def __init__(self, table: Any) -> None:
            self._table = table

        def __getattr__(self, name: str) -> Any:
            return getattr(self._table, name)

        async def optimize(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk is full")

    async with TemporaryDirectory() as tmp:
        index = await TranscriptIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        await index.upsert([_doc("vid1", "android", [1.0, 0.0, 0.0, 0.0])])
        index._table = _RaiseUnrelated(index._table)  # pyright: ignore[reportAttributeAccessIssue]

        with assert_raises(RuntimeError):
            await index.optimize(logger=_logger())


@test()
async def reopening_with_a_different_width_is_refused() -> None:
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        index = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([_doc("vid1", "android", [1.0, 0.0, 0.0, 0.0])])
        with assert_raises(VectorDimMismatchError):
            _ = await TranscriptIndex.open(index_dir=index_dir, vector_dim=_DIM + 1)
