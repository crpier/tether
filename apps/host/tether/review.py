"""Deterministic AI-assisted Review: the host-computed `review_digest`.

Issue #16 layers AI assistance onto the manual Review path without touching the
human-approval gate (ADR-0001: the agent proposes, the human decides). The host
computes everything mechanical — dedup clusters, contradiction *candidates*,
bulk groups, and provenance-calibrated scrutiny — in pure Python; the model only
narrates the result. That split keeps the load-bearing behavior testable
(grouping/flagging, not prose) and the digest read-only by construction, so the
review queue, tether/edit/reject, and the trusted corpus are all unchanged.

The digest is recomputed from live SQLite on each call (ADR-0006): no new
tables, no persisted flags, nothing to invalidate. Contradiction detection is
inherently semantic, and slice 1 has no embeddings/FTS, so the host only
surfaces keyword-overlap *candidates* — the model decides which truly conflict.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import UUID7, BaseModel
from snekql.sqlite import Database, Fetched

from tether.logging import Logger
from tether.memories import Memory, MemoryProvenance, MemoryService
from tether.memory_capabilities import MemoryRead

DEDUP_THRESHOLD = 0.6
"""Token-overlap ratio above which two loose Memories are near-duplicates.

High by design: dedup should cluster restatements of the same fact, not merely
topical neighbours. A one-word edit of a six-word fact still clears this bar.
"""

CONTRADICTION_THRESHOLD = 0.4
"""Token-overlap ratio above which a loose-vs-tethered pair is a contradiction *candidate*.

Looser than dedup: the host cannot tell agreement from disagreement without
semantics, so it casts a wider net of overlapping pairs and lets the model judge
which actually conflict.
"""

_MIN_GROUP_SIZE = 2
"""A grouping (dedup cluster, bulk batch) is only worth surfacing at two or more."""

type Scrutiny = Literal["normal", "elevated"]
"""Whether a queued Memory warrants closer human attention than the default."""


class ReviewQueueItem(MemoryRead):
    """A loose Memory awaiting Review, annotated with its computed scrutiny.

    Reuses every `MemoryRead` field so a client renders it like any Memory, plus
    `scrutiny`: `elevated` when the capture carries a low-trust signal.
    """

    scrutiny: Scrutiny

    @classmethod
    def from_memory(cls, memory: Memory[Fetched]) -> ReviewQueueItem:
        """Render a loose Memory as a queue item, deriving its scrutiny."""
        return cls(
            **MemoryRead.from_memory(memory).model_dump(),
            scrutiny=_scrutiny(memory.provenance),
        )


class DedupGroup(BaseModel):
    """A cluster of two or more near-duplicate loose Memories."""

    memory_ids: list[UUID7]


class BulkGroup(BaseModel):
    """Loose Memories that arrived together in one bulk-capture batch."""

    batch: str
    memory_ids: list[UUID7]


class ContradictionCandidate(BaseModel):
    """A loose Memory that keyword-overlaps a tethered fact; the model judges it."""

    loose_id: UUID7
    tethered_id: UUID7


class ReviewDigest(BaseModel):
    """The full AI-assisted view of the review queue at one point in time."""

    queue: list[ReviewQueueItem]
    dedup_groups: list[DedupGroup]
    bulk_groups: list[BulkGroup]
    contradictions: list[ContradictionCandidate]


_WORD = re.compile(r"[0-9a-z]+")


def _tokenize(content: str) -> frozenset[str]:
    """Lowercase a Memory's content into its set of alphanumeric word tokens.

    Set semantics (not counts) because both dedup and contradiction care about
    which words are shared, not how often — the shared vocabulary is the signal.
    """
    return frozenset(_WORD.findall(content.lower()))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-overlap ratio of two word sets: shared over total, 0 when both empty."""
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _scrutiny(provenance: MemoryProvenance) -> Scrutiny:
    """Elevate scrutiny for low-confidence captures, normal otherwise.

    Kept as a single low-trust check so other signals (e.g. unreliable sources)
    can fold in here later without changing callers.
    """
    if provenance.get("confidence") == "low":
        return "elevated"
    return "normal"


class _UnionFind:
    """Minimal union-find over list indices, for dedup clustering."""

    def __init__(self, size: int) -> None:
        self._parent: list[int] = list(range(size))

    def find(self, node: int) -> int:
        """Return the representative root of `node`, path-compressing on the way."""
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, left: int, right: int) -> None:
        """Merge the sets containing `left` and `right`."""
        self._parent[self.find(left)] = self.find(right)


def _dedup_groups(loose: list[Memory[Fetched]]) -> list[DedupGroup]:
    """Cluster near-duplicate loose Memories via union-find over pairwise overlap.

    Transitivity matters: if A~B and B~C the three share one group even when A
    and C alone fall under the threshold, so the human reviews one cluster.
    """
    tokens = [_tokenize(memory.content) for memory in loose]
    clusters = _UnionFind(len(loose))
    for i in range(len(loose)):
        for j in range(i + 1, len(loose)):
            if _jaccard(tokens[i], tokens[j]) >= DEDUP_THRESHOLD:
                clusters.union(i, j)
    members: dict[int, list[int]] = {}
    for index in range(len(loose)):
        members.setdefault(clusters.find(index), []).append(index)
    return [
        DedupGroup(memory_ids=[loose[index].id for index in indices])
        for indices in members.values()
        if len(indices) >= _MIN_GROUP_SIZE
    ]


def _bulk_groups(loose: list[Memory[Fetched]]) -> list[BulkGroup]:
    """Group loose Memories sharing a provenance batch (≥2), ordered by batch."""
    by_batch: dict[str, list[UUID7]] = {}
    for memory in loose:
        batch = memory.provenance.get("batch")
        if batch is not None:
            by_batch.setdefault(batch, []).append(memory.id)
    return [
        BulkGroup(batch=batch, memory_ids=memory_ids)
        for batch, memory_ids in sorted(by_batch.items())
        if len(memory_ids) >= _MIN_GROUP_SIZE
    ]


def _contradictions(
    loose: list[Memory[Fetched]], tethered: list[Memory[Fetched]]
) -> list[ContradictionCandidate]:
    """Pair each loose Memory with tethered facts it keyword-overlaps."""
    tethered_tokens = [(memory.id, _tokenize(memory.content)) for memory in tethered]
    candidates: list[ContradictionCandidate] = []
    for loose_memory in loose:
        loose_tokens = _tokenize(loose_memory.content)
        for tethered_id, tokens in tethered_tokens:
            if _jaccard(loose_tokens, tokens) >= CONTRADICTION_THRESHOLD:
                candidates.append(
                    ContradictionCandidate(
                        loose_id=loose_memory.id, tethered_id=tethered_id
                    )
                )
    return candidates


class ReviewService:
    """Read-only capability that derives the AI-assisted Review digest.

    Holds only the database: the digest never mutates, so it needs neither the
    KB projection nor the event bus. Every call recomputes from live SQLite.
    """

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def review_digest(self, *, logger: Logger) -> ReviewDigest:
        """Compute dedup, bulk, contradiction, and scrutiny over the live queue.

        Loose Memories form the review queue (newest-first); tethered,
        non-deleted Memories are the corpus contradiction candidates are checked
        against. Soft-deleted Memories are excluded from both.
        """
        logger.debug("Computing review digest")
        async with self.database.transaction() as tx:
            loose = await tx.fetch_all(
                MemoryService.loose_queue().order_by(Memory.created_at.desc())
            )
            tethered = await tx.fetch_all(
                MemoryService.tethered_corpus().order_by(Memory.created_at.desc())
            )
        digest = ReviewDigest(
            queue=[ReviewQueueItem.from_memory(memory) for memory in loose],
            dedup_groups=_dedup_groups(loose),
            bulk_groups=_bulk_groups(loose),
            contradictions=_contradictions(loose, tethered),
        )
        logger.debug(
            "Review digest computed",
            queue_size=len(digest.queue),
            dedup_group_count=len(digest.dedup_groups),
            bulk_group_count=len(digest.bulk_groups),
            contradiction_count=len(digest.contradictions),
        )
        return digest
