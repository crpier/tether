"""Flag-gated web search tools and Tavily provider adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

import httpx2
from pydantic import BaseModel, Field
from snekql.sqlite import Database, Transaction, insert, select, update
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.tool_runtime import ToolSpec
from tether.youtube_quota import QuotaMeta, SystemClock, YouTubeSyncState

_SPEND_KEY_PREFIX = "search_uses"
"""Prefix for persisted UTC-month Tavily credit counters."""
_HTTP_CLIENT_ERROR_FLOOR = 400
"""The lowest HTTP status that represents a failed Tavily request."""


class SearchDepth(StrEnum):
    """Tavily search depth and its corresponding provider credit cost."""

    BASIC = "basic"
    ADVANCED = "advanced"

    @property
    def credit_cost(self) -> int:
        """Provider credits charged by one search at this depth."""
        return 2 if self is SearchDepth.ADVANCED else 1


def _month_key(now: datetime) -> str:
    """The persisted spend key for `now`'s UTC calendar month."""
    return f"{_SPEND_KEY_PREFIX}:{now.astimezone(UTC):%Y-%m}"


class SearchDisabledError(Exception):
    """Web search is unavailable because its explicit configuration gate is off."""

    def __init__(self) -> None:
        super().__init__("web search is disabled")


class SearchUpstreamError(Exception):
    """Tavily could not serve a valid web search response."""

    def __init__(self, status_code: int | None = None) -> None:
        message = (
            "Tavily search request failed"
            if status_code is None
            else f"Tavily search failed (status {status_code})"
        )
        super().__init__(message)


class SearchBudgetExhaustedError(Exception):
    """A search would cross the configured monthly provider-credit cap."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"web search monthly use cap reached ({used}/{limit})")
        self.limit: int = limit
        self.used: int = used


class SearchSpendGuard(Protocol):
    """Reserve provider credits before a Tavily request is sent."""

    async def charge(self, credit_cost: int) -> None:
        """Reserve credits or raise before the upstream request."""
        ...

    async def snapshot(self) -> QuotaMeta | None:
        """Report current usage, or `None` when uncapped."""
        ...


class UnlimitedSearchSpend(SearchSpendGuard):
    """An uncapped spend guard for explicitly injected providers."""

    async def charge(self, credit_cost: int) -> None:
        """Allow spending without a cap."""
        _ = credit_cost

    async def snapshot(self) -> QuotaMeta | None:
        """Return no quota metadata because spending is uncapped."""
        return None


class PersistentSearchSpendGuard(SearchSpendGuard):
    """A hard UTC-calendar-month Tavily credit cap persisted in SQLite."""

    def __init__(
        self, database: Database, *, max_uses: int, clock: SystemClock | None = None
    ) -> None:
        self._clock: SystemClock = clock or SystemClock()
        self._database: Database = database
        self._max_uses: int = max(0, max_uses)

    async def charge(self, credit_cost: int) -> None:
        """Reserve a call's credits, refusing one that would cross the cap."""
        month_key = _month_key(self._clock.now())

        async def _reserve(transaction: Transaction) -> None:
            row = await transaction.fetch_one_or_none(
                select(YouTubeSyncState).where(YouTubeSyncState.key.eq(month_key))
            )
            used = int(row.value) if row is not None else 0
            if used + credit_cost > self._max_uses:
                raise SearchBudgetExhaustedError(used, self._max_uses)
            next_used = str(used + credit_cost)
            if row is None:
                _ = await transaction.execute(
                    insert(YouTubeSyncState(key=month_key, value=next_used))
                )
            else:
                _ = await transaction.execute(
                    update(YouTubeSyncState)
                    .set(YouTubeSyncState.value.to(next_used))
                    .where(YouTubeSyncState.key.eq(month_key))
                )

        async with self._database.transaction(mode="immediate") as tx:
            await _reserve(tx)

    async def snapshot(self) -> QuotaMeta:
        """Read the current month's credit use without reserving more."""
        async with self._database.transaction() as transaction:
            row = await transaction.fetch_one_or_none(
                select(YouTubeSyncState).where(
                    YouTubeSyncState.key.eq(_month_key(self._clock.now()))
                )
            )
        used = int(row.value) if row is not None else 0
        return QuotaMeta(
            limit=self._max_uses,
            remaining=max(0, self._max_uses - used),
            used=used,
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One provider-neutral web search result returned to the agent."""

    extracted_content: str | None
    snippet: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """Provider-neutral results and remaining quota from one web search."""

    results: tuple[SearchResult, ...]
    quota: QuotaMeta | None = None


class SearchProvider(Protocol):
    """The web search capability's swappable provider boundary."""

    async def search(
        self, query: str, *, max_results: int, search_depth: SearchDepth
    ) -> SearchResponse:
        """Search the public web and return provider-neutral results."""
        ...


@dataclass(frozen=True, slots=True)
class TavilySearchRequest:
    """One normalized request sent through the Tavily transport seam."""

    max_results: int
    query: str
    search_depth: SearchDepth
    include_answer: bool = False
    include_raw_content: bool = True


@dataclass(frozen=True, slots=True)
class TavilyResponse:
    """One Tavily HTTP response normalized away from the HTTP client."""

    status_code: int
    payload: Mapping[str, object]


class HttpTavilyTransport:
    """Thin async HTTP transport for Tavily's search endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.tavily.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._timeout_seconds: float = timeout_seconds

    async def search(self, request: TavilySearchRequest) -> TavilyResponse:
        """POST one search and normalize the decoded response."""
        async with httpx2.AsyncClient(
            base_url=self._base_url, timeout=self._timeout_seconds
        ) as client:
            response = await client.post(
                "/search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "include_answer": request.include_answer,
                    "include_raw_content": request.include_raw_content,
                    "max_results": request.max_results,
                    "query": request.query,
                    "search_depth": request.search_depth.value,
                },
            )
        try:
            decoded = response.json()
        except Exception:
            decoded = {}
        payload: Mapping[str, object] = (
            cast("Mapping[str, object]", decoded)
            if isinstance(decoded, Mapping)
            else {}
        )
        return TavilyResponse(status_code=int(response.status_code), payload=payload)


class TavilyTransport(Protocol):
    """The isolated Tavily HTTP boundary, faked by provider tests."""

    async def search(self, request: TavilySearchRequest) -> TavilyResponse:
        """Submit one web search request."""
        ...


class WebSearchParams(BaseModel):
    """Search the current public web and return snippets plus extracted content."""

    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=20)
    search_depth: SearchDepth = SearchDepth.BASIC


class TavilySearchProvider(SearchProvider):
    """The provider-neutral search port implemented with Tavily."""

    def __init__(
        self,
        transport: TavilyTransport,
        *,
        min_request_interval_seconds: float = 0.0,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        spend_guard: SearchSpendGuard | None = None,
    ) -> None:
        self._last_request_at: float | None = None
        self._min_request_interval_seconds: float = max(
            0.0, min_request_interval_seconds
        )
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self.spend_guard: SearchSpendGuard = spend_guard or UnlimitedSearchSpend()
        self._transport: TavilyTransport = transport

    async def search(
        self, query: str, *, max_results: int, search_depth: SearchDepth
    ) -> SearchResponse:
        """Search Tavily and normalize its result records."""
        await self.spend_guard.charge(search_depth.credit_cost)
        if self._min_request_interval_seconds > 0:
            if self._last_request_at is not None:
                wait = self._min_request_interval_seconds - (
                    self._monotonic() - self._last_request_at
                )
                if wait > 0:
                    await self._sleep(wait)
            self._last_request_at = self._monotonic()
        try:
            response = await self._transport.search(
                TavilySearchRequest(
                    max_results=max_results,
                    query=query,
                    search_depth=search_depth,
                )
            )
        except httpx2.RequestError as error:
            raise SearchUpstreamError from error
        if response.status_code >= _HTTP_CLIENT_ERROR_FLOOR:
            raise SearchUpstreamError(response.status_code)
        raw_results = response.payload.get("results")
        normalized_results = (
            cast("list[object]", raw_results) if isinstance(raw_results, list) else []
        )
        results: list[SearchResult] = []
        for raw_result in normalized_results:
            if not isinstance(raw_result, Mapping):
                continue
            result = cast("Mapping[str, object]", raw_result)
            raw_content = result.get("raw_content")
            snippet = result.get("content")
            title = result.get("title")
            url = result.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            results.append(
                SearchResult(
                    extracted_content=(
                        raw_content if isinstance(raw_content, str) else None
                    ),
                    snippet=snippet if isinstance(snippet, str) else "",
                    title=title,
                    url=url,
                )
            )
        return SearchResponse(
            quota=await self.spend_guard.snapshot(), results=tuple(results)
        )


SEARCH_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((SearchBudgetExhaustedError,), "quota_exceeded", 429),
    ErrorRule((SearchDisabledError,), "upstream_error", 503),
    ErrorRule((SearchUpstreamError,), "upstream_error", 502),
)
"""Web-search failures translated onto the tool envelope."""


async def _web_search(
    request: Request,
    query: str,
    max_results: int = 5,
    search_depth: SearchDepth = SearchDepth.BASIC,
) -> CapabilityOutcome:
    """Execute one provider-backed web search for the calling agent."""
    provider = cast("SearchProvider | None", request.app.state.runtime.search_provider)
    if provider is None:
        raise SearchDisabledError
    response = await provider.search(
        query, max_results=max_results, search_depth=search_depth
    )
    return CapabilityOutcome(
        quota=response.quota,
        result=[
            {
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                **(
                    {"extracted_content": result.extracted_content}
                    if result.extracted_content is not None
                    else {}
                ),
            }
            for result in response.results
        ],
    )


SEARCH_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "web_search",
        WebSearchParams,
        bind_params(_web_search),
        SEARCH_ERRORS,
    ),
)
"""The web-search capability exposed as one generated agent tool."""


def internal_search_tool_routes() -> list[Route]:
    """Mount web search under the authenticated internal tool surface."""
    return [spec.route() for spec in SEARCH_TOOL_SPECS]
