"""Provider-neutral contracts and typed failures for external Web Search."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from snekok import Result

from tether.youtube_quota import QuotaMeta


class SearchDepth(StrEnum):
    """Tavily search depth and its corresponding provider credit cost."""

    BASIC = "basic"
    ADVANCED = "advanced"

    @property
    def credit_cost(self) -> int:
        """Provider credits charged by one search at this depth."""
        return 2 if self is SearchDepth.ADVANCED else 1


@dataclass(frozen=True, slots=True)
class SearchBudgetExhaustedFailure:
    """A search would cross the configured monthly provider-credit cap."""

    limit: int
    used: int


@dataclass(frozen=True, slots=True)
class SearchUpstreamFailure:
    """The provider could not serve a valid response."""

    status_code: int | None = None


type WebSearchFailure = SearchBudgetExhaustedFailure | SearchUpstreamFailure
"""Expected operational failures returned by a Web Search provider."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One provider-neutral Web Search result returned to the agent."""

    extracted_content: str | None
    snippet: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """Provider-neutral results and remaining quota from one Web Search."""

    results: tuple[SearchResult, ...]
    quota: QuotaMeta | None = None


class SearchProvider(Protocol):
    """The Web Search capability's swappable provider boundary."""

    async def search(
        self, query: str, *, max_results: int, search_depth: SearchDepth
    ) -> Result[SearchResponse, WebSearchFailure]:
        """Search the public web, returning expected operational failures as data."""
        ...
