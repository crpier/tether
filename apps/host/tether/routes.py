"""HTTP surfaces for reading and operationally rebuilding current Memory."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from fastapi import APIRouter
from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.dreaming import (
    ConversationMemoryRebuildBusyError,
    ConversationMemoryRebuildError,
    DreamingService,
)
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER, SessionRegistry


class MemoryWorkspaceDiagnosticRead(BaseModel):
    """One workspace-scan issue surfaced through the REST API."""

    code: str
    message: str
    path: str


class MemoryTopicQuery(BaseModel):
    """Query and result bound for current canonical Memory Topics."""

    limit: PositiveInt = 50
    q: str = ""


class MemoryContextRequest(BaseModel):
    """Foreground pi identity and current prompt for transient Memory selection."""

    query: str
    session_id: str


class MemoryContextRead(BaseModel):
    """Complete current Topics selected for one foreground model call."""

    context: str


class MemoryTopicRead(BaseModel):
    """One current canonical Topic rendered from its workspace file."""

    body: str
    evidence: list[str]
    path: str
    title: str


class MemoryRebuildRequest(BaseModel):
    """Explicit operator confirmation for rebuilding Conversation Memory."""

    confirmation: Literal["rebuild-conversation-memory"]


class MemoryRebuildRead(BaseModel):
    """Immediate preparation outcome for one Conversation Memory rebuild."""

    preserved_topics: int
    queued_runs: int
    rebuild_run_id: UUID7
    reset_cursors: int
    tombstoned_topics: int


class _MemoryRuntime(Protocol):
    """Runtime dependencies required by read-only Memory surfaces."""

    dreaming_enabled: bool
    dreaming_service: DreamingService
    logger: Logger
    memory_workspace_service: Any
    session_registry: SessionRegistry
    tool_secret: str


def _runtime(request: Request) -> _MemoryRuntime:
    """Read Memory dependencies from the canonical host runtime."""
    return request.app.state.runtime


router = APIRouter()


@router.post(
    "/internal/memory-context",
    response_model=MemoryContextRead,
    include_in_schema=False,
)
async def foreground_memory_context(
    request: Request, body: MemoryContextRequest
) -> Response:
    """Return relevant current Topics to one authenticated live pi session."""
    runtime = _runtime(request)
    offered_secret = request.headers.get(TOOL_AUTH_HEADER, "")
    if not hmac.compare_digest(offered_secret, runtime.tool_secret):
        return JSONResponse({"detail": "invalid tool secret"}, status_code=401)
    if body.session_id not in runtime.session_registry:
        return JSONResponse({"detail": "unknown session"}, status_code=401)
    context = await runtime.memory_workspace_service.render_context(
        body.query,
        limit=8,
        logger=runtime.logger,
    )
    return JSONResponse(MemoryContextRead(context=context).model_dump(mode="json"))


@router.get("/api/memory-topics", response_model=list[MemoryTopicRead])
async def search_memory_topics(
    request: Request, q: str = "", limit: PositiveInt = 50
) -> list[MemoryTopicRead]:
    """Search valid canonical Topic files without stale index dependence."""
    runtime = _runtime(request)
    topics = await runtime.memory_workspace_service.search(
        q,
        limit=limit,
        logger=runtime.logger,
    )
    workspace_root = runtime.memory_workspace_service.workspace_root
    return [
        MemoryTopicRead(
            body=topic.body,
            evidence=list(topic.evidence),
            path=str(topic.path.relative_to(workspace_root)),
            title=topic.title,
        )
        for topic in topics
    ]


@router.post("/api/memory-rebuilds", response_model=MemoryRebuildRead)
async def rebuild_conversation_memory(
    request: Request,
    body: MemoryRebuildRequest,
) -> Response:
    """Rebuild Conversation-derived Memory after explicit confirmation."""
    _ = body
    runtime = _runtime(request)
    if not runtime.dreaming_enabled:
        return JSONResponse({"detail": "dreaming not enabled"}, status_code=404)
    try:
        rebuild = await runtime.dreaming_service.rebuild_conversation_memory(
            logger=runtime.logger,
            now=datetime.now(UTC),
        )
    except ConversationMemoryRebuildBusyError as error:
        return JSONResponse(
            {"detail": f"active Dream run prevents rebuild: {error}"},
            status_code=409,
        )
    except ConversationMemoryRebuildError as error:
        return JSONResponse({"detail": str(error)}, status_code=500)
    return JSONResponse(
        MemoryRebuildRead(
            preserved_topics=rebuild.preserved_topics,
            queued_runs=rebuild.queued_runs,
            rebuild_run_id=rebuild.rebuild_run_id,
            reset_cursors=rebuild.reset_cursors,
            tombstoned_topics=rebuild.tombstoned_topics,
        ).model_dump(mode="json")
    )


@router.get(
    "/api/memory-topics/diagnostics",
    response_model=list[MemoryWorkspaceDiagnosticRead],
)
async def list_workspace_diagnostics(
    request: Request,
) -> list[MemoryWorkspaceDiagnosticRead]:
    """Return workspace diagnostics from current recorded Memory files."""
    runtime = _runtime(request)
    result = await runtime.memory_workspace_service.scan(logger=runtime.logger)
    return [
        MemoryWorkspaceDiagnosticRead(
            code=diagnostic.code,
            message=diagnostic.message,
            path=str(diagnostic.path),
        )
        for diagnostic in result.diagnostics
    ]
