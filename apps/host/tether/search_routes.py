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

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import search_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint
from tether.search_capabilities import SEARCH_ERRORS, FusedSearchResultRead


class SearchQuery(BaseModel):
    """Query string for the fused, cross-source Search.

    >>> SearchQuery(q="aisle").limit
    50
    """

    limit: PositiveInt = 50
    q: str


_translate_domain_errors = translate_domain_errors(SEARCH_ERRORS)


@endpoint(query=SearchQuery, response=FusedSearchResultRead, response_is_list=True)
@_translate_domain_errors
async def search_fused(request: Request, query: SearchQuery) -> Response:
    """Cross-source Search: RRF-fused Memory + Bucket-item arms, source-tagged."""
    outcome = await search_capabilities.search(request, query.q, limit=query.limit)
    return rest_response(outcome)


search_routes: list[Route] = [
    EndpointRoute("/api/search", search_fused, methods=["GET"]),
]
