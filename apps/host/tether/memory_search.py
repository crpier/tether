"""The Memory search seam: the read path plus the per-Memory index hooks.

`MemorySearchService` is the single collaborator the Memory spine talks to for
everything search-related. It composes the three lower-level pieces — the
`Embedder`, the `SearchIndex` read surface, and the `SearchReconciler` (the sole
index writer) — so `MemoryService` depends on one optional seam rather than
three correlated ones, and the reconciler stays write-only:

- `candidates` is the read path: embed the query, run hybrid search, return the
  RRF-ranked `(id, score)` candidates. It deliberately does *not* filter by
  tether/delete state — `MemoryService` re-filters the candidates against SQLite,
  which is where the ADR-0001 invariant (the assistant searches only
  `tethered ∧ ¬deleted` Memories) is enforced. Enforcing it upstream of the
  index means a drifted index (an orphan a missed event left behind) can never
  leak a loose or deleted Memory into a result.
- `index_memory` / `deindex_memory` are the per-Memory latency hooks the spine
  calls at tether / edit / delete so a change is searchable immediately; they
  delegate to the reconciler, whose periodic pass is their correctness backstop.

Both collaborators are reached through narrow Protocols, so the service is fully
testable against fakes with no model and no LanceDB.

>>> search = MemorySearchService(embedder=embedder, index=index, writer=reconciler)
>>> [candidate.id for candidate in await search.candidates("aisle", limit=10, logger=logger)]
[]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from snekql.sqlite import Fetched

    from tether.embeddings import Embedder
    from tether.logging import Logger
    from tether.memories import Memory
    from tether.search_index import SearchCandidate


class SearchQueryPort(Protocol):
    """The read slice of `SearchIndex` the search path needs.

    A Protocol, not the concrete adapter, so the service can be driven by a fake;
    `SearchIndex` satisfies it structurally."""

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[SearchCandidate]: ...


class MemoryIndexWriter(Protocol):
    """The per-Memory write hooks the spine triggers; `SearchReconciler` fits."""

    async def index_memory(
        self, memory: Memory[Fetched], *, logger: Logger
    ) -> None: ...
    async def deindex_memory(self, memory_id: UUID, *, logger: Logger) -> None: ...


class MemorySearchService:
    """Bundles query embedding, hybrid retrieval, and the index write-hooks."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        index: SearchQueryPort,
        writer: MemoryIndexWriter,
    ) -> None:
        self.embedder: Embedder = embedder
        self.index: SearchQueryPort = index
        self.writer: MemoryIndexWriter = writer

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]:
        """Embed the query and return RRF-ranked candidates, unfiltered by state.

        The caller re-filters these ids against SQLite, so the index's tether/
        delete drift can never surface a forbidden Memory."""
        logger.debug("Embedding search query", query_length=len(query), limit=limit)
        vector = await self.embedder.embed_query(query)
        return await self.index.search(text=query, vector=vector, limit=limit)

    async def index_memory(self, memory: Memory[Fetched], *, logger: Logger) -> None:
        """Make a single Memory searchable now (the tether/edit hook)."""
        await self.writer.index_memory(memory, logger=logger)

    async def deindex_memory(self, memory_id: UUID, *, logger: Logger) -> None:
        """Drop a single Memory from the index now (the delete hook)."""
        await self.writer.deindex_memory(memory_id, logger=logger)
