"""Behavior tests for the SearchIndex LanceDB adapter (slice 3).

`SearchIndex` is the *only* importer of `lancedb`/`pyarrow`. It is a thin,
async, hybrid retriever over a derived projection at `<index_dir>/`: native FTS
on `content` (no vector ANN index — vectors are flat-scanned for exact cosine),
fused with the caller-supplied query vector via RRF. It deals in plain domain
shapes (`SearchDocument` in, `SearchCandidate` out) and knows nothing about
Memories, tethering, or SQLite — the `tethered ∧ ¬deleted` invariant is enforced
upstream, not here.

Vectors are hand-built (not embedded) so the two retrieval arms can be probed
independently: a document is found by text it doesn't share with the query, or
by a vector the query text wouldn't reach. No model, no network — these run in
the gate.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, assert_in, assert_not_in, assert_raises, test

from tether.search_index import (
    SearchCandidate,
    SearchDocument,
    SearchIndex,
    VectorDimMismatchError,
)

_DIM = 4


def _doc(content: str, vector: list[float]) -> SearchDocument:
    return SearchDocument(id=uuid4(), content=content, vector=vector)


def _ids(candidates: list[SearchCandidate]) -> set[UUID]:
    return {candidate.id for candidate in candidates}


@test()
async def open_is_idempotent_and_persists_across_reopen() -> None:
    """Opening an existing index dir reuses the same data, no clobber."""
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        kept = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        index = await SearchIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([kept])
        assert_eq(await index.count(), 1)

        reopened = await SearchIndex.open(index_dir=index_dir, vector_dim=_DIM)

        assert_eq(await reopened.count(), 1)
        found = await reopened.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_in(kept.id, _ids(found))


@test()
async def lexical_match_is_returned() -> None:
    """A document matching the query text lexically is retrieved."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        target = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        other = _doc("grocery shopping list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([target, other])

        results = await index.search(
            text="dentist", vector=[0.0, 0.0, 0.0, 1.0], limit=5
        )

        assert_in(target.id, _ids(results))


@test()
async def both_retrieval_arms_feed_the_reranker() -> None:
    """Hybrid fuses a lexical-only hit and a vector-only hit.

    `lexical` shares the query text but is orthogonal to the query vector;
    `semantic` shares the query vector but no query terms. Both must surface,
    proving the FTS arm and the flat-scan vector arm both feed RRF."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        lexical = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        semantic = _doc("totally unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([lexical, semantic])

        results = await index.search(
            text="dentist", vector=[0.0, 1.0, 0.0, 0.0], limit=5
        )

        ids = _ids(results)
        assert_in(lexical.id, ids)
        assert_in(semantic.id, ids)


@test()
async def candidates_carry_a_relevance_score() -> None:
    """Each candidate exposes a finite RRF score; better matches score higher."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        strong = _doc("dentist dentist appointment", [1.0, 0.0, 0.0, 0.0])
        weak = _doc("unrelated grocery words", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([strong, weak])

        results = await index.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        by_id = {candidate.id: candidate.score for candidate in results}
        assert_gt(by_id[strong.id], 0.0)
        assert_gt(by_id[strong.id], by_id[weak.id])


@test()
async def upsert_updates_existing_content() -> None:
    """Re-upserting the same id replaces (not duplicates) the row, and the new
    content becomes searchable.

    Note: hybrid search always surfaces a doc via its vector arm in a tiny
    corpus, so "the old term no longer matches" isn't observable here — the
    replacement is proven by the row count staying at 1 and the *new* term
    retrieving the same id."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        memory_id = uuid4()
        filler = _doc("unrelated grocery note", [0.0, 0.0, 1.0, 0.0])
        await index.upsert(
            [
                SearchDocument(
                    id=memory_id,
                    content="dentist appointment",
                    vector=[1.0, 0.0, 0.0, 0.0],
                ),
                filler,
            ]
        )
        await index.upsert(
            [
                SearchDocument(
                    id=memory_id,
                    content="optometrist appointment",
                    vector=[0.0, 1.0, 0.0, 0.0],
                )
            ]
        )

        assert_eq(await index.count(), 2)
        fresh = await index.search(
            text="optometrist", vector=[0.0, 1.0, 0.0, 0.0], limit=5
        )
        assert_in(memory_id, _ids(fresh))


@test()
async def list_ids_reports_every_indexed_document() -> None:
    """`list_ids` returns exactly the ids currently stored, for orphan diffing."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        assert_eq(await index.list_ids(), set())
        first = _doc("dentist appointment", [1.0, 0.0, 0.0, 0.0])
        second = _doc("grocery list", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([first, second])

        assert_eq(await index.list_ids(), {first.id, second.id})

        await index.remove([first.id])
        assert_eq(await index.list_ids(), {second.id})


@test()
async def remove_drops_documents_from_results() -> None:
    """A removed id no longer appears in search results."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        gone = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        kept = _doc("dentist checkup notes", [0.0, 1.0, 0.0, 0.0])
        await index.upsert([gone, kept])

        await index.remove([gone.id])

        assert_eq(await index.count(), 1)
        results = await index.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        ids = _ids(results)
        assert_not_in(gone.id, ids)
        assert_in(kept.id, ids)


@test()
async def empty_inputs_are_noops() -> None:
    """Upsert/remove with no ids do nothing and never error."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        await index.upsert([])
        await index.remove([uuid4()])
        await index.remove([])

        assert_eq(await index.count(), 0)


@test()
async def search_on_empty_index_returns_nothing() -> None:
    """Searching before anything is indexed yields an empty list, not an error."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)

        results = await index.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_eq(results, [])


@test()
async def rebuild_replaces_the_whole_corpus() -> None:
    """Rebuild drops every prior document and indexes only the new set."""
    async with TemporaryDirectory() as tmp:
        index = await SearchIndex.open(index_dir=Path(tmp) / "index", vector_dim=_DIM)
        old = _doc("dentist appointment tuesday", [1.0, 0.0, 0.0, 0.0])
        await index.upsert([old])
        replacement = _doc("optometrist appointment friday", [0.0, 1.0, 0.0, 0.0])

        await index.rebuild([replacement])

        assert_eq(await index.count(), 1)
        results = await index.search(
            text="dentist", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_not_in(old.id, _ids(results))


@test()
async def reopening_with_a_different_dimension_is_rejected() -> None:
    """A vector_dim that disagrees with the on-disk schema raises, not corrupts.

    The reconciler owns drop-and-rebuild on a model/dimension change; the
    adapter must refuse to silently open a mismatched index."""
    async with TemporaryDirectory() as tmp:
        index_dir = Path(tmp) / "index"
        index = await SearchIndex.open(index_dir=index_dir, vector_dim=_DIM)
        await index.upsert([_doc("dentist appointment", [1.0, 0.0, 0.0, 0.0])])

        with assert_raises(VectorDimMismatchError):
            _ = await SearchIndex.open(index_dir=index_dir, vector_dim=_DIM + 1)
