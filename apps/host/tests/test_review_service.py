"""Behavior tests for the deterministic AI-assisted Review digest.

These drive the `tether.review.ReviewService` seam directly against a real
(in-memory) SQLite database. The digest is pure host computation — dedup
clustering, contradiction *candidates*, bulk grouping, and provenance-calibrated
scrutiny — so every assertion is on grouping/flagging behavior, never on model
prose. Memories are seeded through `MemoryService` (capture with provenance,
tether) exactly as the production producers would.
"""

from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched
from snektest import (
    AsyncFixture,
    assert_eq,
    assert_in,
    assert_not_in,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryProvenance,
    MemoryService,
    create_memory_schema,
)
from tether.review import ReviewDigest, ReviewService


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.review_service")


class ReviewHarness:
    """Seed Memories through the real service, then compute a digest."""

    def __init__(
        self,
        memory_service: MemoryService,
        review_service: ReviewService,
        *,
        logger: Logger,
    ) -> None:
        self.memory_service: MemoryService = memory_service
        self.review_service: ReviewService = review_service
        self.logger: Logger = logger

    async def capture(
        self,
        content: str,
        provenance: MemoryProvenance | None = None,
    ) -> Memory[Fetched]:
        """Capture a loose Memory with optional provenance."""
        return await self.memory_service.capture(
            content, provenance=provenance, logger=self.logger
        )

    async def capture_tethered(self, content: str) -> Memory[Fetched]:
        """Capture and tether a Memory, landing it in the trusted corpus."""
        memory = await self.memory_service.capture(content, logger=self.logger)
        return await self.memory_service.tether(memory, logger=self.logger)

    async def delete(self, memory: Memory[Fetched]) -> Memory[Fetched]:
        """Soft-delete (reject) a Memory."""
        return await self.memory_service.delete(memory, logger=self.logger)

    async def digest(self) -> ReviewDigest:
        """Compute the review digest over current live state."""
        return await self.review_service.review_digest(logger=self.logger)


async def review_harness() -> AsyncFixture[ReviewHarness]:
    """A fresh isolated database with a Memory service and a Review service."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        kb_service = KnowledgeBaseService(kb_root=Path(kb_root))
        yield ReviewHarness(
            MemoryService(database=db, kb_service=kb_service, tracer=noop_tracer()),
            ReviewService(database=db),
            logger=structlog.stdlib.get_logger("test.review_service"),
        )
    await db.close()


def _scrutiny_of(digest: ReviewDigest, memory_id: UUID7) -> str:
    """Return the scrutiny the digest assigned to a queued Memory."""
    scrutiny_by_id = {item.id: item.scrutiny for item in digest.queue}
    assert memory_id in scrutiny_by_id, "memory not present in review queue"
    return scrutiny_by_id[memory_id]


# --- Queue membership ---


@test()
async def queue_contains_loose_memories() -> None:
    """The review queue surfaces Memories still awaiting Review."""
    harness = await load_fixture(review_harness())
    loose = await harness.capture("I think I prefer aisle seats")

    digest = await harness.digest()

    assert_in(loose.id, [item.id for item in digest.queue])


@test()
async def queue_excludes_tethered_memories() -> None:
    """The queue is loose-only: a tethered Memory has left it."""
    harness = await load_fixture(review_harness())
    tethered = await harness.capture_tethered("I prefer window seats")

    digest = await harness.digest()

    assert_not_in(tethered.id, [item.id for item in digest.queue])


@test()
async def queue_excludes_soft_deleted_memories() -> None:
    """A rejected loose Memory drops out of the review queue."""
    harness = await load_fixture(review_harness())
    loose = await harness.capture("a loose memory I will reject")
    _ = await harness.delete(loose)

    digest = await harness.digest()

    assert_not_in(loose.id, [item.id for item in digest.queue])


# --- Provenance-calibrated scrutiny ---


@test()
async def low_confidence_provenance_elevates_scrutiny() -> None:
    """A low-confidence capture warrants closer human attention."""
    harness = await load_fixture(review_harness())
    shaky = await harness.capture(
        "the speaker probably lives in Lisbon",
        MemoryProvenance(kind="youtube", confidence="low"),
    )

    digest = await harness.digest()

    assert_eq(_scrutiny_of(digest, shaky.id), "elevated")


@test()
async def manual_provenance_keeps_normal_scrutiny() -> None:
    """A manually captured Memory carries no elevated-scrutiny signal."""
    harness = await load_fixture(review_harness())
    manual = await harness.capture("I prefer aisle seats")

    digest = await harness.digest()

    assert_eq(_scrutiny_of(digest, manual.id), "normal")


# --- Bulk grouping ---


@test()
async def bulk_groups_cluster_memories_sharing_a_batch() -> None:
    """Memories from one bulk import are grouped so they review as a unit."""
    harness = await load_fixture(review_harness())
    first = await harness.capture(
        "watched a documentary about deep sea fish",
        MemoryProvenance(kind="import", batch="import-2026-06"),
    )
    second = await harness.capture(
        "noted a recipe for sourdough bread",
        MemoryProvenance(kind="import", batch="import-2026-06"),
    )

    digest = await harness.digest()

    batch_groups = {group.batch: set(group.memory_ids) for group in digest.bulk_groups}
    assert_in("import-2026-06", batch_groups)
    assert_eq(batch_groups["import-2026-06"], {first.id, second.id})


@test()
async def bulk_groups_exclude_singleton_batches() -> None:
    """A batch with a single Memory is not a group worth bulk-reviewing."""
    harness = await load_fixture(review_harness())
    _ = await harness.capture(
        "a lone imported fact",
        MemoryProvenance(kind="import", batch="import-singleton"),
    )

    digest = await harness.digest()

    assert_not_in("import-singleton", [group.batch for group in digest.bulk_groups])


# --- Dedup clustering ---


@test()
async def dedup_groups_cluster_near_duplicates() -> None:
    """Near-identical loose Memories cluster into one dedup group."""
    harness = await load_fixture(review_harness())
    first = await harness.capture("I prefer aisle seats on flights")
    second = await harness.capture("I prefer aisle seats on flights please")

    digest = await harness.digest()

    clustered = [
        set(group.memory_ids)
        for group in digest.dedup_groups
        if first.id in group.memory_ids
    ]
    assert_eq(clustered, [{first.id, second.id}])


@test()
async def dedup_groups_exclude_distinct_memories() -> None:
    """Topically unrelated loose Memories are not grouped as duplicates."""
    harness = await load_fixture(review_harness())
    seats = await harness.capture("I prefer aisle seats on flights")
    _ = await harness.capture("I am allergic to penicillin")

    digest = await harness.digest()

    grouped_with_seats = [
        group for group in digest.dedup_groups if seats.id in group.memory_ids
    ]
    assert_eq(grouped_with_seats, [])


@test()
async def dedup_ignores_tethered_memories() -> None:
    """Dedup clusters the loose queue only — tethered corpus is left alone."""
    harness = await load_fixture(review_harness())
    loose = await harness.capture("I prefer aisle seats on flights")
    _ = await harness.capture_tethered("I prefer aisle seats on flights please")

    digest = await harness.digest()

    grouped_with_loose = [
        group for group in digest.dedup_groups if loose.id in group.memory_ids
    ]
    assert_eq(grouped_with_loose, [])


# --- Contradiction candidates ---


@test()
async def contradictions_surface_overlapping_tethered_memory() -> None:
    """A loose Memory overlapping a tethered fact is flagged for the model."""
    harness = await load_fixture(review_harness())
    tethered = await harness.capture_tethered("I live in Berlin")
    loose = await harness.capture("I live in Munich now")

    digest = await harness.digest()

    pairs = {(c.loose_id, c.tethered_id) for c in digest.contradictions}
    assert_in((loose.id, tethered.id), pairs)


@test()
async def contradictions_exclude_unrelated_tethered_memory() -> None:
    """An unrelated tethered fact is not surfaced as a contradiction candidate."""
    harness = await load_fixture(review_harness())
    unrelated = await harness.capture_tethered("I enjoy hiking on weekends")
    loose = await harness.capture("I live in Munich now")

    digest = await harness.digest()

    pairs = {(c.loose_id, c.tethered_id) for c in digest.contradictions}
    assert_not_in((loose.id, unrelated.id), pairs)
