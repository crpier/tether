"""HTTP route for the fused, cross-source Search.

A single new route: `GET /api/search`, distinct from the per-source
`/api/memories/search` and `/api/bucket-items/search` (which remain, for a
caller that already knows which source it wants). This one adapts
`tether.search_capabilities.search` to HTTP the same way `tether.routes` and
`tether.bucket_routes` adapt their own capabilities: `endpoint` validates the
query string, the handler binds it onto the capability execute, and the
outcome is served as a list of source-tagged `FusedSearchResultRead` JSON.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, PositiveInt, field_validator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import search_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint
from tether.search_capabilities import SEARCH_ERRORS, FusedSearchResultRead
from tether.search_fusion import SourceType


class SearchQuery(BaseModel):
    """Query string for the fused, cross-source Search.

    `after`/`before` bound every arm's own capture timestamp, inclusive on
    both ends; either or both may be supplied. `sources`, when supplied,
    restricts fusion to that subset of arms (default: every arm); the query
    string carries it as a comma-separated list (`sources=memory,bucket_item`)
    since the generic query-model parsing this endpoint shares with the rest
    of the host (`tether.openapi._Endpoint`) reads each key once and cannot
    collect a repeated `sources=` key into a list.

    >>> SearchQuery(q="aisle").limit
    50
    >>> SearchQuery(q="aisle", sources="memory,bucket_item").sources
    ['memory', 'bucket_item']
    """

    limit: PositiveInt = 50
    q: str
    sources: list[SourceType] | None = None
    after: AwareDatetime | None = None
    before: AwareDatetime | None = None

    @field_validator("sources", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept `sources` as a comma-separated string from the query string.

        Leaves a list (or `None`) untouched, so passing a genuine list
        programmatically still works."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


_translate_domain_errors = translate_domain_errors(SEARCH_ERRORS)


@endpoint(query=SearchQuery, response=FusedSearchResultRead, response_is_list=True)
@_translate_domain_errors
async def search_fused(request: Request, query: SearchQuery) -> Response:
    """Cross-source Search: RRF-fused Memory + Bucket-item arms, source-tagged."""
    outcome = await search_capabilities.search(
        request,
        query.q,
        limit=query.limit,
        sources=query.sources,
        after=query.after,
        before=query.before,
    )
    return rest_response(outcome)


search_routes: list[Route] = [
    EndpointRoute("/api/search", search_fused, methods=["GET"]),
]
