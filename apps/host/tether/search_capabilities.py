"""The fused Search domain's capability descriptor.

Mirrors `tether.memory_capabilities` / `tether.bucket_capabilities`: the pieces
the REST route (`tether.search_routes`) and the internal tool
(`tether.tools`) both need — the source-tagged Read model and the one execute
function — live here once. The service call itself, `SearchFusionService.search`,
lives in `tether.search_fusion` alongside the fusion engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel
from starlette.requests import Request

from tether.bucket_capabilities import BucketItemRead
from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.memories import EmptySearchQueryError, Memory
from tether.memory_capabilities import MemoryRead
from tether.search_fusion import SourceType

if TYPE_CHECKING:
    from tether.search_fusion import FusedHit

SEARCH_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((EmptySearchQueryError,), "invalid_input", 400),
)
"""The fused Search domain→code map both surfaces translate failures through."""


class FusedSearchResultRead(BaseModel):
    """One fused, source-tagged Search result.

    Exactly one of `memory` / `bucket_item` is populated, matching `source` —
    a discriminated shape so a heterogeneous result list is self-describing
    without a second round trip.

    >>> read = FusedSearchResultRead.from_hit(hit)
    >>> read.source
    'bucket_item'
    """

    source: SourceType
    memory: MemoryRead | None = None
    bucket_item: BucketItemRead | None = None

    @classmethod
    def from_hit(cls, hit: FusedHit) -> FusedSearchResultRead:
        """Render a fused hit as its HTTP representation, tagged by source."""
        if isinstance(hit.item, Memory):
            return cls(source=hit.source, memory=MemoryRead.from_memory(hit.item))
        return cls(source=hit.source, bucket_item=BucketItemRead.from_item(hit.item))


async def search(
    request: Request,
    q: str,
    limit: int = 50,
    facets: dict[str, str] | None = None,
    sources: list[SourceType] | None = None,
) -> CapabilityOutcome:
    """Cross-source Search: RRF-fused Memory + Bucket-item arms, source-tagged.

    `facets` applies only to the Memory arm; `sources`, when supplied,
    restricts fusion to that subset of arms (default: every arm)."""
    hits = await request.app.state.search_fusion_service.search(
        q,
        limit=limit,
        facets=facets,
        sources=sources,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[
            FusedSearchResultRead.from_hit(hit).model_dump(mode="json") for hit in hits
        ]
    )
