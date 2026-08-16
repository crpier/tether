"""Behavior tests for the agent-facing web search tool."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from snekok import Err, Ok, Result
from snektest import assert_eq, test

from tests.surfaces import call_tool, surface_client
from tether.web_search import (
    SearchBudgetExhaustedFailure,
    SearchDepth,
    SearchResponse,
    SearchResult,
    WebSearchFailure,
)
from tether.youtube_quota import QuotaMeta


class ExhaustedSearchProvider:
    """A provider stopped by its pre-request monthly spend guard."""

    async def search(
        self, query: str, *, max_results: int, search_depth: SearchDepth
    ) -> Result[SearchResponse, WebSearchFailure]:
        _ = (query, max_results, search_depth)
        return Err(SearchBudgetExhaustedFailure(limit=1000, used=1000))


class FakeSearchProvider:
    """A web search provider returning one stable result and quota snapshot."""

    async def search(
        self, query: str, *, max_results: int, search_depth: SearchDepth
    ) -> Result[SearchResponse, WebSearchFailure]:
        _ = (query, max_results, search_depth)
        return Ok(
            SearchResponse(
                quota=QuotaMeta(limit=1000, remaining=998, used=2),
                results=(
                    SearchResult(
                        extracted_content="# Async IO\nFull page.",
                        snippet="Python asynchronous IO guide.",
                        title="Async IO",
                        url="https://example.com/async",
                    ),
                ),
            )
        )


@test()
def disabled_web_search_returns_a_well_formed_error_envelope() -> None:
    """The mounted tool reports its off-by-default gate without calling a provider."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(client, "web_search", query="latest news")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "upstream_error")
    assert_eq(envelope["error"]["message"], "web search is disabled")


@test()
def exhausted_web_search_returns_a_quota_error_envelope() -> None:
    """The persisted monthly cap reaches the agent as a quota-specific failure."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory), search_provider=ExhaustedSearchProvider()
        ) as client,
    ):
        envelope = call_tool(client, "web_search", query="latest news")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "quota_exceeded")


@test()
def web_search_returns_inline_content_with_remaining_quota() -> None:
    """A successful call exposes normalized results and post-call quota metadata."""
    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), search_provider=FakeSearchProvider()) as client,
    ):
        envelope = call_tool(
            client,
            "web_search",
            query="Python async IO",
            max_results=5,
            search_depth="advanced",
        )

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        [
            {
                "title": "Async IO",
                "url": "https://example.com/async",
                "snippet": "Python asynchronous IO guide.",
                "extracted_content": "# Async IO\nFull page.",
            }
        ],
    )
    assert_eq(envelope["quota"], {"limit": 1000, "used": 2, "remaining": 998})
