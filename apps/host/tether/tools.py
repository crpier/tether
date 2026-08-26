"""Memory Search and immediate-assimilation internal tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether.active_user_evidence import (
    ActiveUserEvidenceError,
    resolve_active_user_evidence,
)
from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome
from tether.structured_logging import Logger
from tether.tool_runtime import ToolSpec

if TYPE_CHECKING:
    from tether.agent_trace_recorder import AgentTraceRecorder
    from tether.conversations import ConversationService
    from tether.dreaming import DreamingService


class SearchParams(BaseModel):
    """Search Dreaming-maintained current Memory Topics."""

    q: str
    limit: PositiveInt = 50


class QueueMemoryAssimilationParams(BaseModel):
    """No-argument request for immediate post-turn Evidence assimilation."""


class _MemoryToolRuntime(Protocol):
    """Memory read and orchestration dependencies required by foreground tools."""

    conversation_service: ConversationService
    dreaming_service: DreamingService
    logger: Logger
    memory_workspace_service: Any
    trace_recorder: AgentTraceRecorder


def _runtime(request: Request) -> _MemoryToolRuntime:
    """Read Memory-tool dependencies from the canonical host runtime."""
    return cast("_MemoryToolRuntime", request.app.state.runtime)


async def _queue_memory_assimilation(request: Request) -> CapabilityOutcome:
    """Mark this Conversation for immediate assimilation after its turn settles."""
    runtime = _runtime(request)
    try:
        source = await resolve_active_user_evidence(
            conversation_service=runtime.conversation_service,
            trace_recorder=runtime.trace_recorder,
            session_id=request.state.session_id,
        )
    except ActiveUserEvidenceError:
        return CapabilityOutcome(result={"queued": False})
    runtime.dreaming_service.request_immediate_assimilation(source.conversation_id)
    return CapabilityOutcome(result={"queued": True})


async def _search(
    request: Request, q: str, limit: PositiveInt = 50
) -> CapabilityOutcome:
    """Search current Topics and return complete source-backed documents."""
    runtime = _runtime(request)
    topics = await runtime.memory_workspace_service.search(
        q,
        limit=limit,
        logger=runtime.logger,
    )
    workspace_root = runtime.memory_workspace_service.workspace_root
    return CapabilityOutcome(
        result=[
            {
                "body": topic.body,
                "evidence": list(topic.evidence),
                "path": str(topic.path.relative_to(workspace_root)),
                "title": topic.title,
            }
            for topic in topics
        ]
    )


MEMORY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("search", SearchParams, bind_params(_search)),
    ToolSpec(
        "queue_memory_assimilation",
        QueueMemoryAssimilationParams,
        bind_params(_queue_memory_assimilation),
    ),
)
"""Foreground Memory reads plus orchestration; no Memory mutation tools."""


def internal_tool_routes() -> list[Route]:
    """Mount Memory tools as loopback POST endpoints."""
    return [spec.route() for spec in MEMORY_TOOL_SPECS]


__all__ = [
    "MEMORY_TOOL_SPECS",
    "QueueMemoryAssimilationParams",
    "SearchParams",
    "internal_tool_routes",
]
