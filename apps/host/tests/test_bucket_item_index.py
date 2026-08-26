"""Projection tests for the BucketItemIndex over HybridLanceTable.

`BucketItemIndex` is a thin Bucket-item-shaped projection of `HybridLanceTable`
(`BucketItemDocument` in, `BucketItemCandidate` out; no extra payload columns),
a sibling of `SearchIndex` (Memories). The generic retrieval, lifecycle, and
salvage behaviors are proven once in `test_hybrid_lance_table.py`; these tests
pin only what the projection adds — its candidate mapping and its no-argument
self-healing `optimize()` (the reconcile loop calls it without a run-scoped
logger).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import TemporaryDirectory
from snektest import assert_eq, assert_gt, assert_in, test

from tether.bucket_item_index import BucketItemDocument, BucketItemIndex

_DIM = 4

# The lance-internal error message optimize() self-heals on; see
# test_hybrid_lance_table.py for the full salvage behavior suite.
_LANCE_CORRUPTION_MESSAGE = (
    "lance error: Encountered internal error. Please file a bug report at "
    "https://github.com/lance-format/lance/issues. Error decoding batch: "
    "LanceError(Arrow): Invalid argument error: Max offset of 5157331 exceeds "
    "length of values 3031848"
)


class _RaiseOnceOptimize:
    """Wraps a real AsyncTable, raising the lance corruption on first optimize()."""

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
async def a_hit_maps_to_a_bucket_item_candidate() -> None:
    """A search hit surfaces as a `BucketItemCandidate`: the item id + RRF score."""
    async with TemporaryDirectory() as tmp:
        index = await BucketItemIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        item_id = uuid4()
        await index.upsert(
            [
                BucketItemDocument(
                    id=item_id,
                    content="Blade Runner\n2049",
                    vector=[1.0, 0.0, 0.0, 0.0],
                )
            ]
        )

        candidates = await index.search(
            text="Blade Runner", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )

        assert_eq(candidates[0].id, item_id)
        assert_gt(candidates[0].score, 0.0)


@test()
async def optimize_self_heals_the_lance_corruption() -> None:
    """`optimize()` takes no logger yet still salvages the lance internal error.

    The reconcile loop drives this entrypoint on every tick; before the merge
    into `HybridLanceTable` this would have wedged the loop forever."""
    async with TemporaryDirectory() as tmp:
        index = await BucketItemIndex.open(
            index_dir=Path(tmp) / "index", vector_dim=_DIM
        )
        kept = BucketItemDocument(
            id=uuid4(), content="Blade Runner", vector=[1.0, 0.0, 0.0, 0.0]
        )
        await index.upsert([kept])
        # Swap in a table whose first optimize() raises the lance corruption.
        index._table._table = _RaiseOnceOptimize(index._table._table)  # pyright: ignore[reportAttributeAccessIssue]

        await index.optimize()  # self-heals instead of raising

        assert_eq(await index.list_ids(), {kept.id})
        candidates = await index.search(
            text="Blade Runner", vector=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        assert_in(kept.id, {candidate.id for candidate in candidates})
