"""Trusted Memory Search and state-browse read paths."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import Database, Fetched

from tether.memory_store import (
    Memory,
    MemoryState,
    loose_queue,
    tethered_corpus,
)
from tether.structured_logging import Logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tether.search_index import SearchCandidate


class EmptySearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class SearchUnavailableError(Exception):
    """Raised when indexed Search is used without a candidate source."""


class MemoryCandidateSource(Protocol):
    """Ranked candidate source used before canonical SQLite re-filtering."""

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]: ...


class MemorySearchService:
    """Read Memories through trusted SQLite predicates and optional Search candidates.

    Candidate ids carry no trust guarantee. Every indexed result is re-fetched
    from SQLite and filtered to tethered, non-deleted Memories before it leaves
    this service.
    """

    def __init__(
        self,
        database: Database,
        searcher: MemoryCandidateSource | None,
        tracer: Tracer,
    ) -> None:
        self.database: Database = database
        self.searcher: MemoryCandidateSource | None = searcher
        self.tracer: Tracer = tracer

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Hybrid Search the trusted corpus with exact-substring fallback."""
        normalised_query = query.strip()
        if not normalised_query:
            msg = "keyword Search requires a non-empty query"
            raise EmptySearchQueryError(msg)
        with self.tracer.start_as_current_span(
            "MemorySearchService.search",
            attributes={"memory.search.limit": limit},
        ) as span:
            logger.debug("Searching Memories", limit=limit)
            candidates = await self.search_candidates(
                normalised_query, limit=limit, logger=logger
            )
            span.set_attribute("memory.search.candidate_count", len(candidates))
            exact_memories = await self._substring_matches(
                normalised_query, limit=limit, facets=facets, logger=logger
            )
            rank = {
                candidate.id: position for position, candidate in enumerate(candidates)
            }
            semantic_memories = await self.hydrate_tethered(
                list(rank), facets=facets, logger=logger
            )
            semantic_memories.sort(key=lambda memory: rank[memory.id])
            memories: list[Memory[Fetched]] = []
            seen: set[UUID7] = set()
            for memory in [*exact_memories, *semantic_memories]:
                if memory.id in seen:
                    continue
                memories.append(memory)
                seen.add(memory.id)
                if len(memories) >= limit:
                    break
            span.set_attribute("memory.search.result_count", len(memories))
            logger.debug(
                "Memory Search completed",
                limit=limit,
                candidate_count=len(candidates),
                result_count=len(memories),
            )
            return memories

    async def search_candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]:
        """Return ranked index candidates before canonical trust filtering."""
        if self.searcher is None:
            msg = "Memory Search requires a configured candidate source"
            raise SearchUnavailableError(msg)
        return await self.searcher.candidates(query, limit=limit, logger=logger)

    async def hydrate_tethered(
        self,
        ids: Sequence[UUID],
        *,
        facets: Mapping[str, str] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Re-fetch candidate ids through canonical trust and optional filters."""
        logger.debug("Hydrating tethered Memory candidates", candidate_count=len(ids))
        if not ids:
            return []
        query = tethered_corpus().where(Memory.id.in_(*ids))
        if after is not None:
            query = query.where(Memory.tethered_at.gte(after))
        if before is not None:
            query = query.where(Memory.tethered_at.lte(before))
        async with self.database.transaction() as transaction:
            memories = await transaction.fetch_all(query)
        return self._filter_facets(memories, facets)

    async def browse_by_state(
        self,
        state: MemoryState,
        *,
        limit: int | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Browse live Memories by loose or tethered state."""
        logger.debug("Browsing Memories by state", state=state)
        match state:
            case "loose":
                browse = loose_queue().order_by(Memory.created_at.desc())
            case "tethered":
                browse = tethered_corpus().order_by(Memory.tethered_at.desc())
        if limit is not None:
            browse = browse.limit(limit)
        async with self.database.transaction() as transaction:
            memories = await transaction.fetch_all(browse)
        logger.debug(
            "Memory browse completed",
            state=state,
            result_count=len(memories),
        )
        return memories

    async def _substring_matches(
        self,
        query: str,
        *,
        limit: PositiveInt,
        facets: Mapping[str, str] | None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Find trusted Memories containing every visible query term."""
        terms = query.split()
        logger.debug("Searching Memories by visible substring", terms_count=len(terms))
        statement = tethered_corpus().order_by(Memory.tethered_at.desc())
        for term in terms:
            statement = statement.where(Memory.content.like(f"%{term}%"))
        async with self.database.transaction() as transaction:
            memories = await transaction.fetch_all(statement)
        return self._filter_facets(memories, facets)[:limit]

    @staticmethod
    def _filter_facets(
        memories: list[Memory[Fetched]],
        facets: Mapping[str, str] | None,
    ) -> list[Memory[Fetched]]:
        """Apply exact-match facet filters after canonical row hydration."""
        if not facets:
            return memories
        return [
            memory
            for memory in memories
            if all(memory.facets.get(key) == value for key, value in facets.items())
        ]
