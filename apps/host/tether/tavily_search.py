"""Tavily transport and provider adapter for external Web Search."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx2
from snekok import Err, Ok, Result

from tether.search_spend import SearchSpendGuard, UnlimitedSearchSpend
from tether.web_search import (
    SearchDepth,
    SearchProvider,
    SearchResponse,
    SearchResult,
    SearchUpstreamFailure,
    WebSearchFailure,
)

_HTTP_CLIENT_ERROR_FLOOR = 400
"""The lowest HTTP status that represents a failed Tavily request."""


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


class TavilyTransport(Protocol):
    """The isolated Tavily HTTP boundary, faked by provider tests."""

    async def search(self, request: TavilySearchRequest) -> TavilyResponse:
        """Submit one Web Search request."""
        ...


class HttpTavilyTransport:
    """Thin async HTTP transport for Tavily's Search endpoint."""

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
        except ValueError:
            decoded = {}
        payload: Mapping[str, object] = (
            cast("Mapping[str, object]", decoded)
            if isinstance(decoded, Mapping)
            else {}
        )
        return TavilyResponse(status_code=int(response.status_code), payload=payload)


class TavilySearchProvider(SearchProvider):
    """The provider-neutral Web Search port implemented with Tavily."""

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
    ) -> Result[SearchResponse, WebSearchFailure]:
        """Search Tavily and normalize its result records."""
        charged = await self.spend_guard.charge(search_depth.credit_cost)
        if isinstance(charged, Err):
            return Err(charged.error)
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
        except httpx2.RequestError:
            return Err(SearchUpstreamFailure())
        if response.status_code >= _HTTP_CLIENT_ERROR_FLOOR:
            return Err(SearchUpstreamFailure(status_code=response.status_code))
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
        return Ok(
            SearchResponse(
                quota=await self.spend_guard.snapshot(), results=tuple(results)
            )
        )
