"""Read-only execution of saved Synthetic panels against the live corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Database, Fetched

from tether.memory_search import MemorySearchService
from tether.memory_store import Memory, tethered_corpus
from tether.panel_model import EXECUTE_DEFAULT_LIMIT
from tether.panel_store import SyntheticPanel
from tether.structured_logging import Logger

_SEARCH_CANDIDATE_LIMIT = 200
"""Candidate bound before panel facet and window post-filtering."""


@dataclass(frozen=True)
class PanelResults:
    """One execution of a panel: capped rows plus the uncapped match count."""

    memories: list[Memory[Fetched]]
    total: int


class PanelExecutionPort(Protocol):
    """Execute a stored panel without mutating its saved definition."""

    async def execute(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        now: datetime,
        limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
        logger: Logger,
    ) -> PanelResults:
        """Recompute a panel against the trusted corpus."""
        ...


class PanelExecutor:
    """Recompute saved panel queries through canonical Memory Search."""

    def __init__(
        self, database: Database, memory_search: MemorySearchService, tracer: Tracer
    ) -> None:
        self.database: Database = database
        self.memory_search: MemorySearchService = memory_search
        self.tracer: Tracer = tracer

    async def execute(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        now: datetime,
        limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
        logger: Logger,
    ) -> PanelResults:
        """Run a panel's saved query against the trusted corpus, capped.

        The relative window resolves against the caller's `now` on every call.
        Text queries retain hybrid Search ranking; facets-only panels use
        recency-of-trust order. SQLite hydration remains authoritative.
        """
        after = (
            now - timedelta(days=panel.window_days)
            if panel.window_days is not None
            else None
        )
        with self.tracer.start_as_current_span(
            "PanelService.execute",
            attributes={"panel.id": str(panel.id)},
        ) as span:
            logger.debug(
                "Executing Synthetic panel",
                panel_id=str(panel.id),
                name=panel.name,
            )
            if panel.query is not None:
                matches = await self._execute_search(
                    panel.query, panel.facets, after=after, logger=logger
                )
            else:
                matches = await self._execute_listing(
                    panel.facets, after=after, logger=logger
                )
            span.set_attribute("panel.execute.total", len(matches))
            logger.debug(
                "Synthetic panel execution completed",
                panel_id=str(panel.id),
                total=len(matches),
            )
            return PanelResults(memories=matches[:limit], total=len(matches))

    async def _execute_search(
        self,
        query: str,
        facets: dict[str, str],
        *,
        after: datetime | None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Run Search candidates through canonical hydration and rank ordering."""
        candidates = await self.memory_search.search_candidates(
            query, limit=_SEARCH_CANDIDATE_LIMIT, logger=logger
        )
        if not candidates:
            return []
        rank = {candidate.id: position for position, candidate in enumerate(candidates)}
        memories = await self.memory_search.hydrate_tethered(
            list(rank),
            facets=facets or None,
            after=after,
            logger=logger,
        )
        memories.sort(key=lambda memory: rank[memory.id])
        return memories

    async def _execute_listing(
        self,
        facets: dict[str, str],
        *,
        after: datetime | None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """List the trusted corpus most-recently-tethered first."""
        query = tethered_corpus().order_by(Memory.tethered_at.desc())
        if after is not None:
            query = query.where(Memory.tethered_at.gte(after))
        async with self.database.transaction() as transaction:
            memories = await transaction.fetch_all(query)
        _ = logger
        if facets:
            memories = [
                memory
                for memory in memories
                if all(memory.facets.get(key) == value for key, value in facets.items())
            ]
        return memories
