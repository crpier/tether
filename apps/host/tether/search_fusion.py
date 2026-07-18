"""Cross-source Search fusion: N-arm rank fusion, source tagging, diversity caps.

`SearchReconciler.candidates()` and `BucketItemReconciler.candidates()` each
give a ranked list of ids from their own arm, already internally fused (native
FTS + cosine, RRF-reranked inside `HybridLanceTable`). This module sits one
level up: it fuses *across* arms, so one Search can return Memories and Bucket
items ranked together instead of the caller having to know in advance which
kind of thing it is looking for.

Each arm's candidate ids are hydrated and re-filtered through its own service
(Memories: tethered ∧ ¬deleted, plus an optional facet filter; Bucket items:
active-only) before fusion ever sees them — ADR 0009's "the index is only a
candidate generator" applies per arm, exactly as it does for a single-source
Search. Recomputed on every call, never cached (ADR 0006).

Arms have disjoint id spaces (a Memory and a Bucket item are never the same
row), so this is not classic same-item RRF. Instead, each arm's hydrated hit
is scored by its own rank position — a reciprocal-rank score, the same shape
RRF uses — and every arm's scored hits are merged into one list and sorted by
that score. A future arm (anything beyond Memories/Bucket items) only needs to
satisfy the `FusionArm` protocol; `fuse()` itself never changes.

>>> service = SearchFusionService(
...     memory_service=memory_service, bucket_item_service=bucket_item_service
... )
>>> hits = await service.search("aisle seat", logger=logger)
>>> hits[0].source
'memory'
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import PositiveInt
from snekql.sqlite import Fetched

from tether.bucket_items import BucketItem, BucketItemService
from tether.memories import EmptySearchQueryError, Memory, MemoryService

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tether.logging import Logger

type SourceType = Literal["memory", "bucket_item"]
"""Every fused result's arm tag, so a caller can act on a hit without a lookup."""

type FusedItem = Memory[Fetched] | BucketItem[Fetched]
"""The hydrated payload types fusion carries; a future arm extends this union."""

RRF_FUSION_K: int = 60
"""Rank-fusion smoothing constant — the standard RRF discount factor, applied
here to each arm's own rank position rather than to a shared item's rank
across arms. Higher values flatten the gap between a #1 and a #10 hit."""

DEFAULT_DIVERSITY_CAP: int = 30
"""Max hits any one source may contribute to a fused top-K.

A cap, not a quota: a source with fewer matches than this is never topped up.
The single tunable knob for diversity — retuning it is a one-line change here,
not a change to `fuse()`'s logic."""


@dataclass(frozen=True, slots=True)
class FusedHit:
    """One fused, source-tagged Search result."""

    item: FusedItem
    source: SourceType


class _RawCandidate(Protocol):
    """The shape an arm's raw candidate must carry: just an id.

    `fuse()` only ever needs a candidate's rank position (its index in the
    arm's own ranked list), never its raw score — `SearchCandidate` and
    `BucketItemCandidate` both satisfy this structurally."""

    @property
    def id(self) -> UUID: ...


class FusionArm(Protocol):
    """One search source's seam into fusion: a tag, ranked candidates, hydrate.

    Mirrors `SearchIndexPort`'s shape one layer up: a Protocol so a fake arm
    drives fusion tests directly, and a future arm only needs to satisfy this
    to plug in — `fuse()` never has to change."""

    @property
    def source(self) -> SourceType: ...
    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> Sequence[_RawCandidate]: ...
    async def hydrate(
        self, ids: Sequence[UUID], *, logger: Logger
    ) -> Sequence[FusedItem]: ...


@dataclass(frozen=True, slots=True)
class MemoryFusionArm:
    """The Memory arm: `MemoryService`'s searcher plus its tethered re-filter.

    `facets`, when supplied, is bound once per Search call and applies only to
    this arm — the Bucket-item arm's `hydrate` takes no facets, so both arms
    keep the same `FusionArm` shape."""

    facets: Mapping[str, str] | None
    memory_service: MemoryService

    @property
    def source(self) -> SourceType:
        return "memory"

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> Sequence[_RawCandidate]:
        return await self.memory_service.search_candidates(
            query, limit=limit, logger=logger
        )

    async def hydrate(
        self, ids: Sequence[UUID], *, logger: Logger
    ) -> Sequence[FusedItem]:
        return await self.memory_service.hydrate_tethered(
            ids, facets=self.facets, logger=logger
        )


@dataclass(frozen=True, slots=True)
class BucketItemFusionArm:
    """The Bucket-item arm: `BucketItemService`'s searcher plus its active re-filter."""

    bucket_item_service: BucketItemService

    @property
    def source(self) -> SourceType:
        return "bucket_item"

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> Sequence[_RawCandidate]:
        return await self.bucket_item_service.search_candidates(
            query, limit=limit, logger=logger
        )

    async def hydrate(
        self, ids: Sequence[UUID], *, logger: Logger
    ) -> Sequence[FusedItem]:
        return await self.bucket_item_service.hydrate_active(ids, logger=logger)


class SearchFusionService:
    """Fuses the Memory and Bucket-item arms into one ranked, source-tagged list.

    >>> service = SearchFusionService(
    ...     memory_service=memory_service, bucket_item_service=bucket_item_service
    ... )
    >>> hits = await service.search("aisle seat", logger=logger)
    >>> hits[0].source
    'memory'
    """

    def __init__(
        self, *, memory_service: MemoryService, bucket_item_service: BucketItemService
    ) -> None:
        self.memory_service: MemoryService = memory_service
        self.bucket_item_service: BucketItemService = bucket_item_service

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        facets: dict[str, str] | None = None,
        sources: Sequence[SourceType] | None = None,
        logger: Logger,
    ) -> list[FusedHit]:
        """Fused Search over the selected arms (default: every arm).

        `facets` applies only to the Memory arm, exact-match AND, exactly as
        `MemoryService.search` documents. `sources`, when supplied, restricts
        fusion to that subset of arms; omitted or `None` runs every arm."""
        normalised_query = query.strip()
        if not normalised_query:
            msg = "fused Search requires a non-empty query"
            raise EmptySearchQueryError(msg)
        arms = self._build_arms(facets=facets, sources=sources)
        return await fuse(arms, normalised_query, limit=limit, logger=logger)

    def _build_arms(
        self,
        *,
        facets: Mapping[str, str] | None,
        sources: Sequence[SourceType] | None,
    ) -> list[FusionArm]:
        """Build the arm list a call runs, honoring an optional `sources` filter."""
        selected = frozenset(sources) if sources is not None else None
        arms: list[FusionArm] = []
        if selected is None or "memory" in selected:
            arms.append(
                MemoryFusionArm(facets=facets, memory_service=self.memory_service)
            )
        if selected is None or "bucket_item" in selected:
            arms.append(
                BucketItemFusionArm(bucket_item_service=self.bucket_item_service)
            )
        return arms


def _adjust_scores(
    scored: list[tuple[float, FusedHit]],
) -> list[tuple[float, FusedHit]]:
    """Seam for post-fusion score adjustment; identity today.

    Provenance-trust weighting (human-proved > human-asserted > machine-synced)
    and any future time-window score effects both land here, as a rewrite over
    the already rank-fused scores, before diversity capping ever runs."""
    return scored


def _cap_diversity(
    scored: Sequence[tuple[float, FusedHit]], *, limit: int, diversity_cap: int
) -> list[FusedHit]:
    """Fill the top `limit` fused hits, capping any one source's contribution.

    `scored` is already sorted best-first, so a single pass that skips a
    source once it hits its cap preserves true relevance order: a capped
    source's lowest-ranked excess is dropped, and the next-best hit from
    another source fills the slot it freed."""
    counts: dict[SourceType, int] = {}
    capped: list[FusedHit] = []
    for _, hit in scored:
        if len(capped) >= limit:
            break
        if counts.get(hit.source, 0) >= diversity_cap:
            continue
        counts[hit.source] = counts.get(hit.source, 0) + 1
        capped.append(hit)
    return capped


async def fuse(
    arms: Sequence[FusionArm],
    query: str,
    *,
    limit: PositiveInt,
    diversity_cap: int = DEFAULT_DIVERSITY_CAP,
    logger: Logger,
) -> list[FusedHit]:
    """Fuse every arm's ranked, hydrated hits into one capped, source-tagged list.

    Each arm's raw candidates are hydrated and re-filtered through its own
    service, then scored by their position in that *filtered* order — a
    reciprocal-rank score, computed per arm since arms share no ids to jointly
    rank. Every arm's scored hits are merged, sorted by that score (the single
    seam `_adjust_scores` reserves for steps 3-4's provenance/time-window
    adjustments), and finally diversity-capped: the sorted list is walked once,
    counting hits per source, and a source that reaches `diversity_cap` stops
    contributing so the next-best hit from another arm fills the freed slot —
    no arm is topped up to a minimum."""
    scored: list[tuple[float, FusedHit]] = []
    for arm in arms:
        raw_candidates = await arm.candidates(query, limit=limit, logger=logger)
        if not raw_candidates:
            continue
        rank_by_id = {
            candidate.id: position for position, candidate in enumerate(raw_candidates)
        }
        hydrated = await arm.hydrate(list(rank_by_id), logger=logger)
        ranked_items = sorted(hydrated, key=lambda item: rank_by_id[item.id])
        scored.extend(
            (
                1.0 / (RRF_FUSION_K + position + 1),
                FusedHit(source=arm.source, item=item),
            )
            for position, item in enumerate(ranked_items)
        )
    scored = _adjust_scores(scored)
    scored.sort(key=lambda entry: entry[0], reverse=True)
    return _cap_diversity(scored, limit=limit, diversity_cap=diversity_cap)
