"""Behavior tests for the `MemorySearchService` facade.

The facade is the seam the Memory spine talks to for search: it embeds a query,
runs hybrid retrieval, and forwards the per-Memory index hooks. These tests
drive it against fakes of its two collaborators (the index read-port and the
reconciler write-hooks), so no model and no LanceDB are needed — only that the
facade wires the pieces together and preserves the index's ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import structlog
from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import assert_eq, assert_true, test

from tether.embeddings import FakeEmbedder, Vector
from tether.logging import Logger
from tether.memories import Memory, create_memory_schema
from tether.memory_search import MemorySearchService
from tether.search_index import SearchCandidate

_DIM = 16


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.memory_search")


async def _make_memory(content: str) -> Memory[Fetched]:
    """Build a real fetched Memory via an in-memory DB (no cast, no LanceDB)."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with db.transaction() as tx:
        created = await tx.execute(insert(Memory(content=content)).returning())
        memory = await tx.fetch_one_or_none(
            select(Memory).where(Memory.id.eq(created.id))
        )
    assert memory is not None
    await db.close()
    return memory


class RecordingIndex:
    """A fake `SearchQueryPort` that returns canned candidates and records calls."""

    def __init__(self, candidates: list[SearchCandidate]) -> None:
        self.candidates: list[SearchCandidate] = candidates
        self.calls: list[tuple[str, Sequence[float], int]] = []

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[SearchCandidate]:
        self.calls.append((text, vector, limit))
        return self.candidates


class RecordingWriter:
    """A fake `MemoryIndexWriter` that records the hooks the facade delegates."""

    def __init__(self) -> None:
        self.indexed: list[UUID] = []
        self.deindexed: list[UUID] = []

    async def index_memory(self, memory: Memory[Fetched], *, logger: Logger) -> None:
        self.indexed.append(memory.id)

    async def deindex_memory(self, memory_id: UUID, *, logger: Logger) -> None:
        self.deindexed.append(memory_id)


@test()
async def candidates_embeds_the_query_and_forwards_it_to_the_index() -> None:
    """The query is embedded once and handed to the index with text + limit."""
    embedder = FakeEmbedder(vector_dim=_DIM)
    index = RecordingIndex([])
    service = MemorySearchService(
        embedder=embedder, index=index, writer=RecordingWriter()
    )

    _ = await service.candidates("aisle seats", limit=7, logger=_logger())

    assert_eq(len(index.calls), 1)
    text, vector, limit = index.calls[0]
    assert_eq(text, "aisle seats")
    assert_eq(limit, 7)
    expected: Vector = await embedder.embed_query("aisle seats")
    assert_eq(list(vector), expected)


@test()
async def candidates_returns_the_index_ranking_unchanged() -> None:
    """The facade is a pass-through for ranking; it never reorders candidates."""
    ranked = [
        SearchCandidate(id=uuid4(), score=0.9),
        SearchCandidate(id=uuid4(), score=0.4),
    ]
    service = MemorySearchService(
        embedder=FakeEmbedder(vector_dim=_DIM),
        index=RecordingIndex(ranked),
        writer=RecordingWriter(),
    )

    result = await service.candidates("anything", limit=10, logger=_logger())

    assert_eq([candidate.id for candidate in result], [c.id for c in ranked])


@test()
async def index_memory_delegates_to_the_writer() -> None:
    """The tether/edit hook forwards the Memory to the reconciler."""
    writer = RecordingWriter()
    service = MemorySearchService(
        embedder=FakeEmbedder(vector_dim=_DIM),
        index=RecordingIndex([]),
        writer=writer,
    )
    memory = await _make_memory("a tethered fact")

    await service.index_memory(memory, logger=_logger())

    assert_eq(writer.indexed, [memory.id])


@test()
async def deindex_memory_delegates_to_the_writer() -> None:
    """The delete hook forwards the id to the reconciler."""
    writer = RecordingWriter()
    service = MemorySearchService(
        embedder=FakeEmbedder(vector_dim=_DIM),
        index=RecordingIndex([]),
        writer=writer,
    )
    memory_id = uuid4()

    await service.deindex_memory(memory_id, logger=_logger())

    assert_true(memory_id in writer.deindexed)
