"""Agent-tool translation for external Web Search."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, Field
from snekok import Err, Ok
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.tool_runtime import ToolSpec
from tether.web_search import (
    SearchBudgetExhaustedFailure,
    SearchDepth,
    SearchProvider,
    SearchUpstreamFailure,
)


class SearchDisabledError(Exception):
    """Present a disabled Web Search through the tool error translator."""

    def __init__(self) -> None:
        super().__init__("web search is disabled")


class SearchUpstreamError(Exception):
    """Present an upstream failure through the tool error translator."""

    def __init__(self, status_code: int | None = None) -> None:
        message = (
            "Tavily search request failed"
            if status_code is None
            else f"Tavily search failed (status {status_code})"
        )
        super().__init__(message)


class SearchBudgetExhaustedError(Exception):
    """Present provider-credit exhaustion through the tool error translator."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"web search monthly use cap reached ({used}/{limit})")
        self.limit: int = limit
        self.used: int = used


class UnexpectedWebSearchFailureError(Exception):
    """The configured provider violated the closed Web Search failure contract."""


class WebSearchParams(BaseModel):
    """Search the current public web and return snippets plus extracted content."""

    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=20)
    search_depth: SearchDepth = SearchDepth.BASIC


SEARCH_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((SearchBudgetExhaustedError,), "quota_exceeded", 429),
    ErrorRule((SearchDisabledError,), "upstream_error", 503),
    ErrorRule((SearchUpstreamError,), "upstream_error", 502),
)
"""Web Search failures translated onto the tool envelope."""


async def _web_search(
    request: Request,
    query: str,
    max_results: int = 5,
    search_depth: SearchDepth = SearchDepth.BASIC,
) -> CapabilityOutcome:
    """Execute one provider-backed Web Search for the calling agent."""
    provider = cast("SearchProvider | None", request.app.state.runtime.search_provider)
    if provider is None:
        raise SearchDisabledError
    outcome = await provider.search(
        query, max_results=max_results, search_depth=search_depth
    )
    match outcome:
        case Err(SearchBudgetExhaustedFailure(used=used, limit=limit)):
            raise SearchBudgetExhaustedError(used, limit)
        case Err(SearchUpstreamFailure(status_code=status_code)):
            raise SearchUpstreamError(status_code)
        case Ok(response):
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
        case Err(failure):  # pragma: no cover - closed failure union is exhaustive
            message = f"unexpected Web Search failure: {type(failure).__name__}"
            raise UnexpectedWebSearchFailureError(message)


SEARCH_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "web_search",
        WebSearchParams,
        bind_params(_web_search),
        SEARCH_ERRORS,
    ),
)
"""The Web Search capability exposed as one generated agent tool."""


def internal_search_tool_routes() -> list[Route]:
    """Mount Web Search under the authenticated internal tool surface."""
    return [spec.route() for spec in SEARCH_TOOL_SPECS]
