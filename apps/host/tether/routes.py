"""Read-only HTTP surfaces over Dreaming-maintained Memory Topics."""

from __future__ import annotations

import hmac
from typing import Any, Protocol

from fastapi import APIRouter
from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


class _MemoryRuntime(Protocol):
    """Runtime dependencies required by read-only Memory surfaces."""

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
