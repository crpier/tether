"""Behavior tests for the cross-source Search fusion engine (`fuse()`).

These exercise `tether.search_fusion.fuse()` directly against minimal stub
arms (no database, no embedder) so the fusion algorithm itself — cross-arm
rank ordering, source tagging, and diversity capping — is pinned down without
the service-seam machinery `test_search_fusion_service.py` covers separately.

Each stub arm hydrates to real (detached) `Memory` / `BucketItem` rows —
`fuse()`'s `FusedItem` union is exactly those two types, the same way
`memory_capabilities._memory_reference` builds a detached `Memory` carrying
only the fields a caller needs — so a stub item's `content` / `title` doubles
as its test label.

Assertions target externally observable behavior only (which ids appear, in
what order, tagged with which source) — never the internal RRF score
arithmetic, per the spec's testing decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid7

import structlog
from snektest import assert_eq, test

from tether.bucket_items import BucketItem, Fetched
from tether.logging import Logger
from tether.memories import Memory, MemoryProvenance
from tether.search_fusion import FusedHit, FusedItem, SourceType, fuse


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.search_fusion")


def _memory(
    label: str, *, provenance: MemoryProvenance | None = None
) -> Memory[Fetched]:
    """A detached Memory carrying only what `fuse()` needs: id + content."""
    return cast(
        "Memory[Fetched]",
        Memory.construct(
            content=label,
            id=uuid7(),
            version=1,
            provenance=provenance or MemoryProvenance(kind="manual"),
        ),
    )


def _bucket_item(label: str) -> BucketItem[Fetched]:
    """A detached Bucket item carrying only what `fuse()` needs: id + title."""
    return cast(
        "BucketItem[Fetched]",
        BucketItem.construct(
            id=uuid7(),
            version=1,
            item_type="movie",
            title=label,
            dedup_key=label.lower(),
            data={},
            intent_context="",
        ),
    )


@dataclass(frozen=True, slots=True)
class _StubCandidate:
    """A minimal `_RawCandidate`-shaped stub: just an id."""

    id: UUID


class StubArm:
    """A `FusionArm` driven by a fixed, caller-supplied ranked item list.

    `hydrate` looks each requested id up in `items`, silently dropping any id
    absent from it — the same shape a real arm's SQLite re-filter takes when a
    candidate id no longer exists or fails the arm's own state filter."""

    def __init__(self, source: SourceType, ranked_items: list[FusedItem]) -> None:
        self.source: SourceType = source
        self._ranked_ids: list[UUID] = [item.id for item in ranked_items]
        self._items: dict[UUID, FusedItem] = {item.id: item for item in ranked_items}

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[_StubCandidate]:
        del query, logger
        return [
            _StubCandidate(id=identifier) for identifier in self._ranked_ids[:limit]
        ]

    async def hydrate(self, ids: Sequence[UUID], *, logger: Logger) -> list[FusedItem]:
        del logger
        return [
            self._items[identifier] for identifier in ids if identifier in self._items
        ]


def _labels(hits: list[FusedHit]) -> list[str]:
    """Extract each hit's label (a Memory's `content` or a Bucket item's `title`)."""
    return [
        hit.item.content if isinstance(hit.item, Memory) else hit.item.title
        for hit in hits
    ]


@test()
async def fuse_interleaves_two_arms_by_rank_position() -> None:
    """Cross-arm fusion orders by each hit's own rank position, not by arm.

    Every arm's #1 hit scores ahead of every arm's #2 hit regardless of
    source, so the fused order runs "round by rank" across arms; equal-score
    ties (same rank, different arms) keep the arms' call order stable."""
    memory_arm = StubArm("memory", [_memory("m1"), _memory("m2"), _memory("m3")])
    bucket_arm = StubArm("bucket_item", [_bucket_item("b1"), _bucket_item("b2")])

    hits = await fuse([memory_arm, bucket_arm], "query", limit=10, logger=_logger())

    assert_eq(_labels(hits), ["m1", "b1", "m2", "b2", "m3"])


@test()
async def fuse_tags_every_hit_with_its_arm_source() -> None:
    """Every fused hit carries the source of the arm it came from."""
    memory_arm = StubArm("memory", [_memory("m1")])
    bucket_arm = StubArm("bucket_item", [_bucket_item("b1")])

    hits = await fuse([memory_arm, bucket_arm], "query", limit=10, logger=_logger())

    sources = {
        label: hit.source for label, hit in zip(_labels(hits), hits, strict=True)
    }
    assert_eq(sources, {"m1": "memory", "b1": "bucket_item"})


@test()
async def fuse_caps_an_over_represented_source() -> None:
    """A source that dominates raw candidates is capped, not the whole list.

    5 Memory hits vs. 1 Bucket-item hit, capped at 2 per source: the lowest-
    ranked Memory excess (m3, m4, m5) is dropped and the Bucket-item hit
    survives — diversity capping, not a relevance re-ranking."""
    memory_arm = StubArm(
        "memory",
        [_memory("m1"), _memory("m2"), _memory("m3"), _memory("m4"), _memory("m5")],
    )
    bucket_arm = StubArm("bucket_item", [_bucket_item("b1")])

    hits = await fuse(
        [memory_arm, bucket_arm],
        "query",
        limit=10,
        diversity_cap=2,
        logger=_logger(),
    )

    assert_eq(_labels(hits), ["m1", "b1", "m2"])


@test()
async def fuse_never_tops_up_a_source_below_its_cap() -> None:
    """A source with fewer matches than the cap is left as-is — a cap, not a quota."""
    memory_arm = StubArm("memory", [_memory("m1")])
    bucket_arm = StubArm("bucket_item", [_bucket_item("b1")])

    hits = await fuse(
        [memory_arm, bucket_arm],
        "query",
        limit=10,
        diversity_cap=10,
        logger=_logger(),
    )

    assert_eq(_labels(hits), ["m1", "b1"])


@test()
async def fuse_skips_an_arm_with_no_candidates() -> None:
    """An arm returning no candidates (an empty or never-reconciled index)
    contributes nothing, and the other arm's hits still come back whole."""
    memory_arm = StubArm("memory", [_memory("m1")])
    empty_bucket_arm = StubArm("bucket_item", [])

    hits = await fuse(
        [memory_arm, empty_bucket_arm], "query", limit=10, logger=_logger()
    )

    assert_eq(_labels(hits), ["m1"])


@test()
async def fuse_truncates_the_combined_result_to_limit() -> None:
    """`limit` caps the fused total across every arm, not each arm individually.

    Two arms each contribute a hit at rank 0 and rank 1; `limit=2` keeps only
    the two best-ranked hits overall (the rank-0 pair), dropping both arms'
    rank-1 hit even though neither arm alone exceeded `limit`."""
    memory_arm = StubArm("memory", [_memory("m0"), _memory("m1")])
    bucket_arm = StubArm("bucket_item", [_bucket_item("b0"), _bucket_item("b1")])

    hits = await fuse([memory_arm, bucket_arm], "query", limit=2, logger=_logger())

    assert_eq(_labels(hits), ["m0", "b0"])


@test()
async def human_proved_memory_outranks_human_asserted_for_equal_match_strength() -> (
    None
):
    """A Memory tethered by proving retention through Recall outranks one that
    was merely typed by hand, when both match the query equally well."""
    proved = _memory("proved", provenance=MemoryProvenance(kind="manual"))
    asserted = _memory("asserted", provenance=MemoryProvenance(kind="manual"))
    memory_arm = StubArm("memory", [asserted, proved])

    hits = await fuse(
        [memory_arm],
        "query",
        limit=10,
        human_proved_memory_ids=frozenset({proved.id}),
        logger=_logger(),
    )

    assert_eq(_labels(hits), ["proved", "asserted"])


@test()
async def human_asserted_memory_outranks_machine_synced_for_equal_match_strength() -> (
    None
):
    """A manually-captured Memory outranks an equally-matching synced one."""
    synced = _memory("synced", provenance=MemoryProvenance(kind="youtube"))
    asserted = _memory("asserted", provenance=MemoryProvenance(kind="manual"))
    memory_arm = StubArm("memory", [synced, asserted])

    hits = await fuse([memory_arm], "query", limit=10, logger=_logger())

    assert_eq(_labels(hits), ["asserted", "synced"])


@test()
async def bucket_item_outranks_machine_synced_memory_for_equal_match_strength() -> None:
    """Bucket items sit at the human-asserted tier, above machine-synced Memories."""
    synced = _memory("synced", provenance=MemoryProvenance(kind="web"))
    item = _bucket_item("item")
    memory_arm = StubArm("memory", [synced])
    bucket_arm = StubArm("bucket_item", [item])

    hits = await fuse([memory_arm, bucket_arm], "query", limit=10, logger=_logger())

    assert_eq(_labels(hits), ["item", "synced"])


@test()
async def a_memory_without_a_completed_study_item_is_not_human_proved() -> None:
    """`human_proved_memory_ids` gates the boost — an absent id keeps a Memory
    at its ordinary derived tier, ranked by match strength alone."""
    plain = _memory("plain", provenance=MemoryProvenance(kind="manual"))
    memory_arm = StubArm("memory", [plain])

    hits = await fuse(
        [memory_arm],
        "query",
        limit=10,
        human_proved_memory_ids=frozenset(),
        logger=_logger(),
    )

    assert_eq(_labels(hits), ["plain"])


@test()
async def trust_weighting_composes_with_diversity_capping() -> None:
    """Trust reorders results first; diversity capping then trims per source,
    so a lower-trust Memory can still be dropped in favor of a higher-trust
    Bucket item once both compete for the same capped slots."""
    proved = _memory("proved", provenance=MemoryProvenance(kind="manual"))
    synced_1 = _memory("synced1", provenance=MemoryProvenance(kind="youtube"))
    synced_2 = _memory("synced2", provenance=MemoryProvenance(kind="youtube"))
    memory_arm = StubArm("memory", [synced_1, synced_2, proved])
    bucket_arm = StubArm("bucket_item", [_bucket_item("item")])

    hits = await fuse(
        [memory_arm, bucket_arm],
        "query",
        limit=10,
        diversity_cap=1,
        human_proved_memory_ids=frozenset({proved.id}),
        logger=_logger(),
    )

    assert_eq(_labels(hits), ["proved", "item"])
