"""The Memory domain's capability descriptor.

The pieces the REST routes (`tether.routes`) and the internal tools
(`tether.tools`) both need live here once: the `MemoryRead` model, the
detached-reference builder, the domain→code map (`MEMORY_ERRORS`), and one
execute function per capability — the service call plus its Read-model
rendering. Each surface derives its own shape from these: REST serves
`result` at a status code, the tool seam wraps the whole outcome in the
uniform envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt, StringConstraints
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.memories import (
    FacetOverviewEntry,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryService,
)
from tether.memory_search import EmptySearchQueryError, MemorySearchService
from tether.memory_store import (
    Memory,
    MemoryProvenance,
    MemoryState,
)
from tether.structured_logging import get_request_logger

type MemoryContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


def _mutation_service(request: Request) -> MemoryService:
    """Read Memory mutation operations from the canonical host runtime."""
    return cast("MemoryService", request.app.state.runtime.memory_service)


def _search_service(request: Request) -> MemorySearchService:
    """Read trusted Memory Search from the canonical host runtime."""
    return cast("MemorySearchService", request.app.state.runtime.memory_search_service)


MEMORY_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((MemoryNotFoundError,), "not_found", 404, detail="memory not found"),
    ErrorRule((MemoryConflictError,), "conflict", 409),
    ErrorRule((EmptySearchQueryError,), "invalid_input", 400),
)
"""The Memory domain→code map both surfaces translate failures through."""


class MemoryRead(BaseModel):
    """HTTP representation of a Memory, exposing its derived trust `state`.

    >>> read = MemoryRead(
    ...     content="I prefer aisle seats",
    ...     created_at=datetime(2026, 1, 1),
    ...     facets={},
    ...     id="018f0000-0000-7000-8000-000000000000",
    ...     state="loose",
    ...     tethered_at=None,
    ...     updated_at=datetime(2026, 1, 1),
    ...     version=1,
    ... )
    >>> read.state
    'loose'
    """

    content: str
    created_at: datetime
    facets: dict[str, str]
    id: UUID7
    state: MemoryState
    tethered_at: datetime | None
    updated_at: datetime
    version: PositiveInt

    @classmethod
    def from_memory(cls, memory: Memory[Fetched]) -> MemoryRead:
        """Render a stored Memory as its HTTP representation.

        A Memory's `state` is derived, not stored: a stamped `tethered_at`
        means a human has vetted it, so it reads as `tethered`.
        """
        return cls(
            content=memory.content,
            created_at=memory.created_at,
            facets=memory.facets,
            id=memory.id,
            state="tethered" if memory.tethered_at is not None else "loose",
            tethered_at=memory.tethered_at,
            updated_at=memory.updated_at,
            version=memory.version,
        )


def _memory_reference(memory_id: UUID, version: PositiveInt) -> Memory[Fetched]:
    """Build a detached Memory carrying only the identity a mutation acts on.

    The service's tether/edit/delete read just `id` and `version` to run their
    optimistic-concurrency check and then re-fetch the live row, so a hand-built
    reference is enough. `content` is a required column with no role on this
    path, hence the empty placeholder.
    """
    return cast(
        "Memory[Fetched]",
        Memory.construct(content="", id=memory_id, version=version),
    )


def _single(memory: Memory[Fetched]) -> CapabilityOutcome:
    """Render a single-Memory outcome, surfacing its provenance."""
    return CapabilityOutcome(
        result=MemoryRead.from_memory(memory).model_dump(mode="json"),
        provenance=memory.provenance,
    )


def _many(memories: list[Memory[Fetched]]) -> CapabilityOutcome:
    """Render a Memory collection; provenance is null for collections."""
    return CapabilityOutcome(
        result=[
            MemoryRead.from_memory(memory).model_dump(mode="json")
            for memory in memories
        ]
    )


async def capture(
    request: Request,
    content: str,
    facets: dict[str, str] | None = None,
    provenance: MemoryProvenance | None = None,
) -> CapabilityOutcome:
    """Capture a loose Memory.

    `provenance` defaults to manual (the text-capture path); a non-manual
    human-asserted producer, such as a transcribed voice note, passes its own
    origin so Review can calibrate scrutiny. Either way the Memory lands loose.
    """
    memory = await _mutation_service(request).capture(
        content,
        facets=facets,
        provenance=provenance,
        logger=get_request_logger(request),
    )
    return _single(memory)


async def browse(
    request: Request, state: MemoryState, limit: int | None = None
) -> CapabilityOutcome:
    """Filter the review queue (`loose`) or browse the corpus (`tethered`)."""
    memories = await _search_service(request).browse_by_state(
        state,
        limit=limit,
        logger=get_request_logger(request),
    )
    return _many(memories)


async def search(
    request: Request,
    q: str,
    limit: int = 50,
    facets: dict[str, str] | None = None,
) -> CapabilityOutcome:
    """Keyword Search over tethered Memories, optionally exact-match filtered by facets."""
    memories = await _search_service(request).search(
        q,
        limit=limit,
        facets=facets,
        logger=get_request_logger(request),
    )
    return _many(memories)


async def tether(
    request: Request, memory_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Promote a loose Memory to tethered."""
    memory = await _mutation_service(request).tether(
        _memory_reference(memory_id, version),
        logger=get_request_logger(request),
    )
    return _single(memory)


async def edit(
    request: Request,
    memory_id: UUID,
    content: str,
    version: PositiveInt,
    facets: dict[str, str] | None = None,
) -> CapabilityOutcome:
    """Edit a Memory's `content`; a human edit keeps trust.

    `facets`, when supplied, replaces the stored Commons facet set verbatim;
    omitted, it leaves facets unchanged.
    """
    memory = await _mutation_service(request).edit_content(
        _memory_reference(memory_id, version),
        content,
        facets=facets,
        logger=get_request_logger(request),
    )
    return _single(memory)


async def agent_edit(
    request: Request,
    memory_id: UUID,
    content: str,
    version: PositiveInt,
    facets: dict[str, str] | None = None,
) -> CapabilityOutcome:
    """Edit a loose Memory; tethered Memories require append, not overwrite."""
    observed_memory = _memory_reference(memory_id, version)
    current_memory = await _mutation_service(request).fetch_active(
        observed_memory.id,
        logger=get_request_logger(request),
    )
    if current_memory.tethered_at is not None:
        msg = "agent cannot overwrite tethered Memory content; append instead"
        raise MemoryConflictError(msg)
    memory = await _mutation_service(request).edit_content(
        observed_memory,
        content,
        facets=facets,
        logger=get_request_logger(request),
    )
    return _single(memory)


async def append(
    request: Request,
    memory_id: UUID,
    content: str,
    version: PositiveInt,
) -> CapabilityOutcome:
    """Append a marked, verbatim block to a Memory."""
    memory = await _mutation_service(request).append_content(
        _memory_reference(memory_id, version),
        content,
        logger=get_request_logger(request),
    )
    return _single(memory)


async def reject(
    request: Request, memory_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Soft-delete (reject) a Memory."""
    memory = await _mutation_service(request).delete(
        _memory_reference(memory_id, version),
        logger=get_request_logger(request),
    )
    return _single(memory)


async def facet_overview(request: Request) -> CapabilityOutcome:
    """Report distinct Commons facet keys/values and how many Memories carry each."""
    entries: list[FacetOverviewEntry] = await _mutation_service(request).facet_overview(
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


async def rename_facet_key(
    request: Request, old_key: str, new_key: str
) -> CapabilityOutcome:
    """Bulk-rename a Commons facet key. Requires prior explicit chat approval."""
    changed_count = await _mutation_service(request).rename_facet_key(
        old_key,
        new_key,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(result={"changed_count": changed_count})


async def merge_facet_value(
    request: Request, key: str, old_value: str, new_value: str
) -> CapabilityOutcome:
    """Bulk-rewrite a Commons facet value. Requires prior explicit chat approval."""
    changed_count = await _mutation_service(request).merge_facet_value(
        key,
        old_value,
        new_value,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(result={"changed_count": changed_count})
