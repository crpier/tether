"""Behavior tests for the generic HybridLanceTable adapter.

`HybridLanceTable` is the *only* importer of `lancedb`/`pyarrow`: one embedded
LanceDB table shaped as `id` + optional string payload columns + FTS'd `content`
+ a fixed-size `vector`, with hybrid retrieval (native FTS fused with flat-scan
cosine via RRF) and the self-healing `optimize`. `SearchIndex` and
`TranscriptIndex` are thin domain projections over it, so every behavior proven
here holds for both stores.

Vectors are hand-built (not embedded) so the two retrieval arms can be probed
independently: a document is found by text it doesn't share with the query, or
by a vector the query text wouldn't reach. No model, no network — these run in
the gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog
from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, assert_in, assert_not_in, assert_raises, test

from tether.hybrid_lance_table import (
    HybridLanceTable,
    TableDocument,
    TableHit,
    VectorDimMismatchError,
)

if TYPE_CHECKING:
    from tether.logging import Logger

_DIM = 4
_TABLE_NAME = "documents"

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
    return structlog.stdlib.get_logger("test.hybrid_lance_table")


async def _open(index_dir: Path) -> HybridLanceTable:
    return await HybridLanceTable.open(
        index_dir=index_dir, table_name=_TABLE_NAME, vector_dim=_DIM
    )


def _doc(content: str, vector: list[float]) -> TableDocument:
    return TableDocument(id=uuid4(), content=content, vector=vector)


def _ids(hits: list[TableHit]) -> set[UUID]:
    return {hit.id for hit in hits}


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


@test()
async def open_is_idempotent_and_persists_across_reopen() -> None:
    """Opening an existing index dir reuses the same data, no clobber."""
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        kept = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        table = await _open(index_dir)
        await table.upsert([kept])
        assert_eq(await table.count(), 1)

        reopened = await _open(index_dir)

        assert_eq(await reopened.count(), 1)
        found = await reopened.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_in(kept.id, _ids(found))


@test()
async def lexical_match_is_returned() -> None:
    """A document matching the query text lexically is retrieved."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        target = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        other = _doc("grocery shopping list", [0.0, 1.0, 0.0, 0.0])
        await table.upsert([target, other])

        results = await table.search(
            text="dentist", vector=[0.0, 0.0, 0.0, 1.0], limit=5
        )

        assert_in(target.id, _ids(results))


@test()
async def a_quoted_phrase_query_does_not_crash_the_search() -> None:
    """A phrase query (quoted terms) must resolve, not error.

    LanceDB phrase queries need an FTS index built with token positions; without
    them the search raises `position is not found but required for phrase
    queries`. The table opts into positions so quoted queries are valid.
    """
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        target = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        await table.upsert([target])

        results = await table.search(
            text='"dentist appointment"', vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_in(target.id, _ids(results))


@test()
async def both_retrieval_arms_feed_the_reranker() -> None:
    """Hybrid fuses a lexical-only hit and a vector-only hit.

    `lexical` shares the query text but is orthogonal to the query vector;
    `semantic` shares the query vector but no query terms. Both must surface,
    proving the FTS arm and the flat-scan vector arm both feed RRF."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        lexical = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        semantic = _doc("totally unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await table.upsert([lexical, semantic])

        results = await table.search(
            text="dentist", vector=[0.0, 1.0, 0.0, 0.0], limit=5
        )

        ids = _ids(results)
        assert_in(lexical.id, ids)
        assert_in(semantic.id, ids)


@test()
async def hits_carry_a_relevance_score() -> None:
    """Each hit exposes a finite RRF score; better matches score higher."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        strong = _doc("dentist dentist appointment", [1.0, 0.0, 0.0, 0.0])
        weak = _doc("unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await table.upsert([strong, weak])

        results = await table.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        by_id = {hit.id: hit.score for hit in results}
        assert_gt(by_id[strong.id], 0.0)
        assert_gt(by_id[strong.id], by_id[weak.id])


@test()
async def hits_carry_their_content_back() -> None:
    """A hit returns the stored `content`, so projections can surface snippets."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        target = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        await table.upsert([target])

        results = await table.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_eq(results[0].content, "dentist appointment tuesday")


@test()
async def payload_columns_round_trip_through_hits() -> None:
    """Extra string payload columns are stored and returned on every hit."""
    async with TemporaryDirectory() as tmp:
        table = await HybridLanceTable.open(
            index_dir=Path(tmp) / "index",
            table_name=_TABLE_NAME,
            vector_dim=_DIM,
            payload_columns=("video_id",),
        )
        chunk = TableDocument(
            id=uuid4(),
            content="android developer registration",
            vector=[1.0, 0.0, 0.0, 0.0],
            payload={"video_id": "vid1"},
        )
        await table.upsert([chunk])

        results = await table.search(
            text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_eq(results[0].payload, {"video_id": "vid1"})


@test()
async def upsert_updates_existing_content() -> None:
    """Re-upserting the same id replaces (not duplicates) the row, and the new
    content becomes searchable.

    Note: hybrid search always surfaces a doc via its vector arm in a tiny
    corpus, so "the old term no longer matches" isn't observable here — the
    replacement is proven by the row count staying at 1 and the *new* term
    retrieving the same id."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        document_id = uuid4()
        filler = _doc("unrelated grocery note", [0.0, 0.0, 1.0, 0.0])
        await table.upsert(
            [
                TableDocument(
                    id=document_id,
                    content="dentist appointment",
                    vector=[1.0, 0.0, 0.0, 0.0],
                ),
                filler,
            ]
        )
        await table.upsert(
            [
                TableDocument(
                    id=document_id,
                    content="optometrist appointment",
                    vector=[0.0, 1.0, 0.0, 0.0],
                )
            ]
        )

        assert_eq(await table.count(), 2)
        fresh = await table.search(
            text="optometrist", vector=[0.0, 1.0, 0.0, 0.0], limit=5
        )
        assert_in(document_id, _ids(fresh))


@test()
async def list_ids_reports_every_indexed_document() -> None:
    """`list_ids` returns exactly the ids currently stored, for orphan diffing."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        assert_eq(await table.list_ids(), set())
        first = _doc("dentist appointment", [1.0, 0.0, 0.0, 0.0])
        second = _doc("grocery list", [0.0, 1.0, 0.0, 0.0])
        await table.upsert([first, second])

        assert_eq(await table.list_ids(), {first.id, second.id})

        await table.remove([first.id])
        assert_eq(await table.list_ids(), {second.id})


@test()
async def remove_drops_documents_from_results() -> None:
    """A removed id no longer appears in search results."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        gone = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        kept = _doc("dentist checkup notes", [0.0, 1.0, 0.0, 0.0])
        await table.upsert([gone, kept])

        await table.remove([gone.id])

        assert_eq(await table.count(), 1)
        results = await table.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        ids = _ids(results)
        assert_not_in(gone.id, ids)
        assert_in(kept.id, ids)


@test()
async def empty_inputs_are_noops() -> None:
    """Upsert/remove with no ids do nothing and never error."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        await table.upsert([])
        await table.remove([uuid4()])
        await table.remove([])

        assert_eq(await table.count(), 0)


@test()
async def search_on_empty_table_returns_nothing() -> None:
    """Searching before anything is indexed yields an empty list, not an error."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")

        results = await table.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_eq(results, [])


@test()
async def rebuild_replaces_the_whole_corpus() -> None:
    """Rebuild drops every prior document and indexes only the new set."""
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        old = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        await table.upsert([old])
        replacement = _doc("optometrist appointment friday", [0.0, 1.0, 0.0, 0.0])

        await table.rebuild([replacement])

        assert_eq(await table.count(), 1)
        results = await table.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_not_in(old.id, _ids(results))


@test()
async def reopening_with_a_different_dimension_is_rejected() -> None:
    """A vector_dim that disagrees with the on-disk schema raises, not corrupts.

    The reconciler owns drop-and-rebuild on a model/dimension change; the
    adapter must refuse to silently open a mismatched table."""
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        table = await _open(index_dir)
        await table.upsert([_doc("dentist appointment", [1.0, 0.0, 0.0, 0.0])])

        with assert_raises(VectorDimMismatchError):
            _ = await HybridLanceTable.open(
                index_dir=index_dir, table_name=_TABLE_NAME, vector_dim=_DIM + 1
            )


@test()
async def optimize_self_heals_a_corrupt_fragment_without_losing_rows() -> None:
    """A lance compaction-decode bug must not wedge the table forever.

    The fragment's rows still read, so optimize() catches the internal error and
    rewrites the table from those rows: no data lost, no re-embedding, and the
    next optimize succeeds on the healthy dataset.
    """
    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        kept = [
            _doc("android developer signing", [1.0, 0.0, 0.0, 0.0]),
            _doc("grocery shopping list", [0.0, 1.0, 0.0, 0.0]),
        ]
        await table.upsert(kept)
        before = await table.list_ids()
        # Swap in a table whose first optimize() raises the lance corruption.
        table._table = _RaiseOnceOptimize(table._table)  # pyright: ignore[reportAttributeAccessIssue]

        await table.optimize(logger=_logger())  # self-heals instead of raising

        assert_eq(await table.list_ids(), before)
        # The rewrite left a healthy dataset: a follow-up optimize is clean, and
        # search still resolves against the salvaged rows.
        await table.optimize(logger=_logger())
        found = await table.search(text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5)
        assert_in(kept[0].id, _ids(found))


@test()
async def salvage_preserves_payload_columns() -> None:
    """The salvage rewrite round-trips payload columns, not just id/content."""
    async with TemporaryDirectory() as tmp:
        table = await HybridLanceTable.open(
            index_dir=Path(tmp) / "index",
            table_name=_TABLE_NAME,
            vector_dim=_DIM,
            payload_columns=("video_id",),
        )
        chunk = TableDocument(
            id=uuid4(),
            content="android developer signing",
            vector=[1.0, 0.0, 0.0, 0.0],
            payload={"video_id": "vid1"},
        )
        await table.upsert([chunk])
        table._table = _RaiseOnceOptimize(table._table)  # pyright: ignore[reportAttributeAccessIssue]

        await table.optimize(logger=_logger())

        results = await table.search(
            text="android", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_eq(results[0].payload, {"video_id": "vid1"})


@test()
async def optimize_reraises_errors_that_are_not_lance_corruption() -> None:
    """A non-corruption failure must propagate, not silently rebuild the table."""

    class _RaiseUnrelated:
        def __init__(self, table: Any) -> None:
            self._table = table

        def __getattr__(self, name: str) -> Any:
            return getattr(self._table, name)

        async def optimize(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk is full")

    async with TemporaryDirectory() as tmp:
        table = await _open(Path(tmp) / "index")
        await table.upsert([_doc("android", [1.0, 0.0, 0.0, 0.0])])
        table._table = _RaiseUnrelated(table._table)  # pyright: ignore[reportAttributeAccessIssue]

        with assert_raises(RuntimeError):
            await table.optimize(logger=_logger())
