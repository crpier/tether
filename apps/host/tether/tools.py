"""Memory and fused-Search declarations for the internal tool surface."""

from __future__ import annotations

from typing import Protocol, cast

from pydantic import UUID7, AwareDatetime, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome
from tether.memory_capabilities import MEMORY_ERRORS, MemoryContent
from tether.memory_capabilities import agent_edit as agent_edit_memory
from tether.memory_capabilities import append as append_memory
from tether.memory_capabilities import browse as browse_memories
from tether.memory_capabilities import capture as capture_memory
from tether.memory_capabilities import facet_overview as facet_overview_memories
from tether.memory_capabilities import merge_facet_value as merge_facet_value_memories
from tether.memory_capabilities import reject as reject_memory
from tether.memory_capabilities import rename_facet_key as rename_facet_key_memories
from tether.memory_capabilities import tether as tether_memory
from tether.memory_store import MemoryState
from tether.review import ReviewService
from tether.search_capabilities import SEARCH_ERRORS
from tether.search_capabilities import search as search_fused
from tether.search_fusion import SourceType
from tether.structured_logging import get_request_logger
from tether.tool_runtime import ToolSpec


class CaptureParams(BaseModel):
    """Params for capturing a loose Memory."""

    content: MemoryContent
    facets: dict[str, str] | None = None


class TetherParams(BaseModel):
    """Params for promoting a loose Memory to tethered."""

    memory_id: UUID7
    version: PositiveInt


class EditParams(BaseModel):
    """Params for editing a loose Memory's content at an observed version.

    The agent must not overwrite tethered Memory content in place. Use `append`
    for routed human-authored additions to trusted Memories.
    """

    memory_id: UUID7
    content: MemoryContent
    version: PositiveInt
    facets: dict[str, str] | None = None


class AppendParams(BaseModel):
    """Params for appending agent-routed verbatim content to a Memory."""

    memory_id: UUID7
    content: MemoryContent
    version: PositiveInt


class RejectParams(BaseModel):
    """Params for soft-deleting (rejecting) a Memory at an observed version."""

    memory_id: UUID7
    version: PositiveInt


class BrowseParams(BaseModel):
    """Params for the review queue (`loose`) / corpus browse (`tethered`)."""

    state: MemoryState
    limit: PositiveInt = 50


class SearchParams(BaseModel):
    """Params for the assistant's cross-source Search (Memories + Bucket items).

    `facets`, when supplied, is an exact-match AND filter applied to the
    Memory arm only: a Memory must carry every given key with exactly that
    value to be returned. `sources`, when supplied, restricts fusion to that
    subset of arms; omitted, every arm runs. `after`/`before`, when supplied,
    bound every arm's own capture timestamp (a Memory's `tethered_at`, a
    Bucket item's `created_at`), inclusive on both ends; either or both may
    be given, and supplying `after` later than `before` is rejected.
    """

    q: str
    limit: PositiveInt = 50
    facets: dict[str, str] | None = None
    sources: list[SourceType] | None = None
    after: AwareDatetime | None = None
    before: AwareDatetime | None = None


class ReviewDigestParams(BaseModel):
    """Params for the AI-assisted Review digest.

    The digest is computed over the whole live queue, so it takes no inputs
    beyond the session identity the gate already requires.
    """


class FacetOverviewParams(BaseModel):
    """Params for the Commons facet overview: distinct keys/values with counts.

    Read-only; takes no inputs beyond the session identity the gate already
    requires.
    """


class RenameFacetKeyParams(BaseModel):
    """Params for bulk-renaming a Commons facet key across every Memory that carries it.

    Destructive to the old key name across the whole corpus: the assistant
    must obtain the user's explicit approval in chat before calling this tool.
    """

    old_key: str
    new_key: str


class MergeFacetValueParams(BaseModel):
    """Params for bulk-rewriting a Commons facet value across every Memory that carries it.

    Destructive to the old value across the whole corpus: the assistant must
    obtain the user's explicit approval in chat before calling this tool.
    """

    key: str
    old_value: str
    new_value: str


class _MemoryToolRuntime(Protocol):
    """Memory-specific dependencies required by Memory tool handlers."""

    review_service: ReviewService


def _runtime(request: Request) -> _MemoryToolRuntime:
    """Read Memory-tool dependencies from the canonical host runtime."""
    return cast("_MemoryToolRuntime", request.app.state.runtime)


async def _review_digest(request: Request) -> CapabilityOutcome:
    """Compute the read-only AI-assisted Review digest."""
    digest = await _runtime(request).review_service.review_digest(
        logger=get_request_logger(request)
    )
    return CapabilityOutcome(result=digest.model_dump(mode="json"))


MEMORY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("capture", CaptureParams, bind_params(capture_memory), MEMORY_ERRORS),
    ToolSpec("browse", BrowseParams, bind_params(browse_memories), MEMORY_ERRORS),
    ToolSpec("search", SearchParams, bind_params(search_fused), SEARCH_ERRORS),
    ToolSpec("review_digest", ReviewDigestParams, bind_params(_review_digest)),
    ToolSpec("tether", TetherParams, bind_params(tether_memory), MEMORY_ERRORS),
    ToolSpec("edit", EditParams, bind_params(agent_edit_memory), MEMORY_ERRORS),
    ToolSpec("append", AppendParams, bind_params(append_memory), MEMORY_ERRORS),
    ToolSpec("reject", RejectParams, bind_params(reject_memory), MEMORY_ERRORS),
    ToolSpec(
        "facet_overview",
        FacetOverviewParams,
        bind_params(facet_overview_memories),
    ),
    ToolSpec(
        "rename_facet_key",
        RenameFacetKeyParams,
        bind_params(rename_facet_key_memories),
        MEMORY_ERRORS,
    ),
    ToolSpec(
        "merge_facet_value",
        MergeFacetValueParams,
        bind_params(merge_facet_value_memories),
        MEMORY_ERRORS,
    ),
)
"""Memory and fused-Search tools in generated-file order."""


def internal_tool_routes() -> list[Route]:
    """Mount Memory and fused-Search tools as loopback POST endpoints."""
    return [spec.route() for spec in MEMORY_TOOL_SPECS]


__all__ = [
    "MEMORY_TOOL_SPECS",
    "AppendParams",
    "BrowseParams",
    "CaptureParams",
    "EditParams",
    "FacetOverviewParams",
    "MergeFacetValueParams",
    "RejectParams",
    "RenameFacetKeyParams",
    "ReviewDigestParams",
    "SearchParams",
    "TetherParams",
    "internal_tool_routes",
]
