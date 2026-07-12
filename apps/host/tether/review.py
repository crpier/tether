"""Deterministic AI-assisted Review: the host-computed `review_digest`.

Issue #16 layers AI assistance onto the manual Review path without touching the
human-approval gate (ADR-0001: the agent proposes, the human decides). The host
computes everything mechanical — dedup clusters, contradiction *candidates*,
bulk groups, and provenance-calibrated scrutiny — in pure Python; the model only
narrates the result. That split keeps the load-bearing behavior testable
(grouping/flagging, not prose) and the digest read-only by construction, so the
review queue, tether/edit/reject, and the trusted corpus are all unchanged.

The digest is recomputed from live SQLite on each call: no new tables, no
persisted flags, nothing to invalidate. Dedup and contradiction detection are
inherently semantic, so when the host is wired with an `Embedder` the digest
recalls and ranks candidates by cosine similarity over Memory vectors — reusing
each tethered Memory's canonical stored vector when it is fresh and embedding
the rest on the fly, never writing any vector back. Without an embedder both
fall back to keyword overlap. Either way the host only surfaces *candidates*:
the model decides which truly conflict, and the human decides what happens.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Literal

from pydantic import UUID7, BaseModel
from snekql.sqlite import Database, Fetched

from tether.embeddings import Embedder, Vector, vector_from_bytes
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

SEMANTIC_DEDUP_THRESHOLD = 0.9
"""Cosine similarity above which two loose Memories are near-duplicates.

Tight for the same reason the keyword bar is high: dedup should cluster
restatements of one fact, which embed almost identically, while merely topical
neighbours land visibly lower in every text-embedding space.
"""

SEMANTIC_CONTRADICTION_FLOOR = 0.6
"""Cosine similarity below which a loose-vs-tethered pair is never surfaced.

A recall floor, not a verdict: it only prunes clear non-neighbours (unrelated
pairs sit noticeably lower) before the per-Memory ranking picks the nearest
tethered facts for the model to judge.
"""

MAX_CONTRADICTION_CANDIDATES_PER_MEMORY = 3
"""Nearest tethered facts surfaced per loose Memory on the semantic path.

Embeddings recall and rank the neighbours, the model judges conflict, and the
human decides — a short shortlist keeps the digest bounded as the trusted
corpus grows, where the keyword path relied on overlap being rare.
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
    """A loose Memory near a tethered fact (semantically or by keyword overlap).

    A recall result, not a verdict: the model judges whether the pair truly
    conflicts, and the human decides what to do about it.
    """

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


def _cosine(left: Vector, right: Vector) -> float:
    """Cosine similarity of two vectors; 0 when either carries no magnitude.

    Computed with explicit norms rather than assuming unit vectors, so the
    digest is correct for any `Embedder`, normalized or not.
    """
    left_norm = math.sqrt(sum(component * component for component in left))
    right_norm = math.sqrt(sum(component * component for component in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    shared = sum(
        left_component * right_component
        for left_component, right_component in zip(left, right, strict=True)
    )
    return shared / (left_norm * right_norm)


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


def _cluster_duplicates(
    loose: list[Memory[Fetched]], is_duplicate: Callable[[int, int], bool]
) -> list[DedupGroup]:
    """Cluster near-duplicate loose Memories via union-find over pairwise checks.

    Transitivity matters: if A~B and B~C the three share one group even when A
    and C alone fall under the threshold, so the human reviews one cluster. The
    duplicate check is injected because the keyword and semantic paths differ
    only in how a pair is compared, never in how clusters form.
    """
    clusters = _UnionFind(len(loose))
    for i in range(len(loose)):
        for j in range(i + 1, len(loose)):
            if is_duplicate(i, j):
                clusters.union(i, j)
    members: dict[int, list[int]] = {}
    for index in range(len(loose)):
        members.setdefault(clusters.find(index), []).append(index)
    return [
        DedupGroup(memory_ids=[loose[index].id for index in indices])
        for indices in members.values()
        if len(indices) >= _MIN_GROUP_SIZE
    ]


def _dedup_groups(loose: list[Memory[Fetched]]) -> list[DedupGroup]:
    """Cluster loose Memories whose word sets overlap enough to be restatements."""
    tokens = [_tokenize(memory.content) for memory in loose]
    return _cluster_duplicates(
        loose, lambda i, j: _jaccard(tokens[i], tokens[j]) >= DEDUP_THRESHOLD
    )


def _semantic_dedup_groups(
    loose: list[Memory[Fetched]], vectors: list[Vector]
) -> list[DedupGroup]:
    """Cluster loose Memories whose vectors sit close enough to be restatements."""
    return _cluster_duplicates(
        loose, lambda i, j: _cosine(vectors[i], vectors[j]) >= SEMANTIC_DEDUP_THRESHOLD
    )


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


def _semantic_contradictions(
    loose: list[Memory[Fetched]],
    loose_vectors: list[Vector],
    tethered: list[Memory[Fetched]],
    tethered_vectors: list[Vector],
) -> list[ContradictionCandidate]:
    """Pair each loose Memory with its nearest tethered facts, nearest first.

    Recall-and-rank, never a verdict: everything past the floor competes, the
    shortlist is capped per loose Memory, and the model judges what conflicts.
    The sort is stable, so equally similar facts keep the corpus's newest-first
    order and the digest stays deterministic for a given state.
    """
    candidates: list[ContradictionCandidate] = []
    for loose_memory, loose_vector in zip(loose, loose_vectors, strict=True):
        nearest = sorted(
            (
                (similarity, tethered_memory.id)
                for tethered_memory, tethered_vector in zip(
                    tethered, tethered_vectors, strict=True
                )
                if (similarity := _cosine(loose_vector, tethered_vector))
                >= SEMANTIC_CONTRADICTION_FLOOR
            ),
            key=lambda scored: scored[0],
            reverse=True,
        )[:MAX_CONTRADICTION_CANDIDATES_PER_MEMORY]
        candidates.extend(
            ContradictionCandidate(loose_id=loose_memory.id, tethered_id=tethered_id)
            for _, tethered_id in nearest
        )
    return candidates


class ReviewService:
    """Read-only capability that derives the AI-assisted Review digest.

    Holds the database plus an optional embedder: the digest never mutates, so
    it needs neither the KB projection nor the event bus. Every call recomputes
    from live SQLite.
    """

    def __init__(self, database: Database, embedder: Embedder | None = None) -> None:
        self.database: Database = database
        self.embedder: Embedder | None = embedder
        """Semantic recall seam for dedup and contradiction candidates.

        `None` when the host runs without search; the digest then falls back to
        keyword overlap, so Review works identically minus semantic recall."""

    async def review_digest(self, *, logger: Logger) -> ReviewDigest:
        """Compute dedup, bulk, contradiction, and scrutiny over the live queue.

        Loose Memories form the review queue (newest-first); tethered,
        non-deleted Memories are the corpus contradiction candidates are checked
        against. Soft-deleted Memories are excluded from both.
        """
        logger.debug("Computing review digest", semantic=self.embedder is not None)
        async with self.database.transaction() as tx:
            loose = await tx.fetch_all(
                MemoryService.loose_queue().order_by(Memory.created_at.desc())
            )
            tethered = await tx.fetch_all(
                MemoryService.tethered_corpus().order_by(Memory.created_at.desc())
            )
        if self.embedder is None:
            dedup_groups = _dedup_groups(loose)
            contradictions = _contradictions(loose, tethered)
        else:
            loose_vectors, tethered_vectors = await self._digest_vectors(
                self.embedder, loose=loose, tethered=tethered
            )
            dedup_groups = _semantic_dedup_groups(loose, loose_vectors)
            contradictions = _semantic_contradictions(
                loose, loose_vectors, tethered, tethered_vectors
            )
        digest = ReviewDigest(
            queue=[ReviewQueueItem.from_memory(memory) for memory in loose],
            dedup_groups=dedup_groups,
            bulk_groups=_bulk_groups(loose),
            contradictions=contradictions,
        )
        logger.debug(
            "Review digest computed",
            queue_size=len(digest.queue),
            dedup_group_count=len(digest.dedup_groups),
            bulk_group_count=len(digest.bulk_groups),
            contradiction_count=len(digest.contradictions),
        )
        return digest

    @staticmethod
    async def _digest_vectors(
        embedder: Embedder,
        *,
        loose: list[Memory[Fetched]],
        tethered: list[Memory[Fetched]],
    ) -> tuple[list[Vector], list[Vector]]:
        """Resolve one vector per Memory without ever writing one back.

        A tethered Memory reuses its canonical stored vector when it matches the
        current content version; loose Memories (which the search reconciler
        never embeds) and stale tethered rows are embedded on the fly in a
        single batch. The fresh vectors are used for this digest and dropped —
        persisting them is the reconciler's job, keeping the digest read-only.
        """
        stored_tethered = [
            vector_from_bytes(memory.embedding)
            if memory.embedding is not None
            and memory.embedded_version == memory.version
            else None
            for memory in tethered
        ]
        owed_contents = [memory.content for memory in loose] + [
            memory.content
            for memory, stored in zip(tethered, stored_tethered, strict=True)
            if stored is None
        ]
        fresh_vectors = iter(
            await embedder.embed_documents(owed_contents) if owed_contents else []
        )
        loose_vectors = [next(fresh_vectors) for _ in loose]
        tethered_vectors = [
            stored if stored is not None else next(fresh_vectors)
            for stored in stored_tethered
        ]
        return loose_vectors, tethered_vectors
