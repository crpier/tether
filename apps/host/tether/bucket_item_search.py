"""Bucket Item Search reads rehydrated through canonical SQLite state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Database, Fetched, select

from tether.bucket_item_store import BucketItem, BucketItemState
from tether.structured_logging import Logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from tether.bucket_item_index import BucketItemCandidate


class EmptyBucketSearchQueryError(Exception):
    """Raised when Bucket Item Search receives a blank query."""


class BucketSearchUnavailableError(Exception):
    """Raised when Bucket Item Search has no configured candidate source."""


class BucketItemCandidateSource(Protocol):
    """Ranked candidate source for canonical Bucket Item Search."""

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[BucketItemCandidate]: ...


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


class BucketItemSearchService:
    """Search and browse Bucket Items while enforcing canonical lifecycle state."""

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
        searcher: BucketItemCandidateSource | None = None,
    ) -> None:
        self.database: Database = database
        self.searcher: BucketItemCandidateSource | None = searcher
        self.tracer: Tracer = tracer

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """Ranked hybrid Search over active Bucket items.

        The query is embedded and run through
        the index's lexical + semantic arms, fused by RRF; the ranked candidate
        ids are then re-fetched from SQLite and re-filtered to active-only
        (non-completed, non-deleted). Results keep the index's relevance order,
        capped at `limit` (default 50)."""
        normalised_query = query.strip()
        if not normalised_query:
            msg = "keyword Search requires a non-empty query"
            raise EmptyBucketSearchQueryError(msg)
        with self.tracer.start_as_current_span(
            "BucketItemSearchService.search",
            attributes={"bucket_item.search.limit": limit},
        ) as span:
            _debug(logger, "Searching Bucket items", limit=limit)
            candidates = await self.search_candidates(
                normalised_query, limit=limit, logger=logger
            )
            span.set_attribute("bucket_item.search.candidate_count", len(candidates))
            if not candidates:
                _debug(
                    logger,
                    "Bucket item Search completed",
                    limit=limit,
                    candidate_count=0,
                    result_count=0,
                )
                return []
            rank = {
                candidate.id: position for position, candidate in enumerate(candidates)
            }
            items = await self.hydrate_active(list(rank), logger=logger)
            items.sort(key=lambda item: rank[item.id])
            span.set_attribute("bucket_item.search.result_count", len(items))
            _debug(
                logger,
                "Bucket item Search completed",
                limit=limit,
                candidate_count=len(candidates),
                result_count=len(items),
            )
            return items

    async def search_candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[BucketItemCandidate]:
        """Raw ranked candidate ids from the index, unfiltered by lifecycle state.

        The read half of the fusion seam (`tether.search_fusion`): a caller
        doing its own cross-source ranking needs candidates before the SQLite
        re-filter, whereas `search` does both steps in one call. Assumes
        `query` is already non-empty."""
        if self.searcher is None:
            msg = "BucketItemSearchService.search_candidates requires a configured searcher"
            raise BucketSearchUnavailableError(msg)
        return await self.searcher.candidates(query, limit=limit, logger=logger)

    async def hydrate_active(
        self,
        ids: Sequence[UUID],
        *,
        after: datetime | None = None,
        before: datetime | None = None,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """Re-fetch candidate ids from SQLite, filtered to active-only (+window).

        The shared re-filter step `search` and fusion both need: candidate ids
        from the index carry no guarantee they're still active rows, so this
        is where the per-arm canonical re-filter happens. `after`/`before`, when
        supplied, bound `created_at` (inclusive) — Bucket items have no
        trust-date equivalent, so their creation timestamp is the capture
        moment a time window bounds. A narrow window can shrink the hydrated
        set below the candidate count the index returned; callers do not
        re-fetch to compensate, mirroring the Memory arm's facet-filter
        behavior. Result order is not preserved — callers sort by their own
        candidate ranking."""
        _debug(
            logger, "Hydrating active Bucket item candidates", candidate_count=len(ids)
        )
        if not ids:
            return []
        query = select(BucketItem).where(
            BucketItem.completed_at.is_null()
            & BucketItem.deleted_at.is_null()
            & BucketItem.id.in_(*ids)
        )
        if after is not None:
            query = query.where(BucketItem.created_at.gte(after))
        if before is not None:
            query = query.where(BucketItem.created_at.lte(before))
        async with self.database.transaction() as tx:
            return await tx.fetch_all(query)

    async def browse_by_state(
        self,
        state: BucketItemState,
        *,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """List Bucket items in a given lifecycle state, newest-first.

        `active` is the live list; `completed` and `deleted` are the retained
        history dedup reasons over. Each is ordered by the timestamp that defines
        the state (creation for active, the terminal stamp otherwise), newest
        first."""
        _debug(logger, "Browsing Bucket items by state", state=state)
        match state:
            case "active":
                browse = (
                    select(BucketItem)
                    .where(
                        BucketItem.completed_at.is_null()
                        & BucketItem.deleted_at.is_null()
                    )
                    .order_by(BucketItem.created_at.desc())
                )
            case "completed":
                browse = (
                    select(BucketItem)
                    .where(
                        BucketItem.completed_at.is_not_null()
                        & BucketItem.deleted_at.is_null()
                    )
                    .order_by(BucketItem.completed_at.desc())
                )
            case "deleted":
                browse = (
                    select(BucketItem)
                    .where(BucketItem.deleted_at.is_not_null())
                    .order_by(BucketItem.deleted_at.desc())
                )
        async with self.database.transaction() as tx:
            items = await tx.fetch_all(browse)
        _debug(
            logger,
            "Bucket item browse completed",
            state=state,
            result_count=len(items),
        )
        return items
