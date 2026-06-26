"""Demo Starlette app wiring toy endpoints through the route contract layer.

Run with `just host` (uvicorn) and open `/docs` to exercise the layer
manually. State is an in-memory dict; it resets on every reload.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid7

from pydantic import UUID7, PositiveInt
from starlette import status
from starlette.applications import Starlette
from starlette.requests import Request

from tether.api import (
    ApiError,
    ApiModel,
    ApiMount,
    ApiRouter,
    BodyParam,
    PathParam,
    QueryParam,
)

_TOOL_SECRET = "secret-tool-token"  # noqa: S105 - demo-only static token


class MemoryState(Enum):
    """Lifecycle state of a Memory."""

    LOOSE = "loose"
    TETHERED = "tethered"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """In-memory persistence record. Kept separate from the API DTO."""

    captured_at: datetime
    content: str
    memory_id: UUID
    state: MemoryState
    version: int


@dataclass(frozen=True, slots=True)
class PublicRequestCtx:
    """Per-request context for the public surface."""

    user_name: str


@dataclass(frozen=True, slots=True)
class ToolRequestCtx:
    """Per-request context for the internal tool surface."""

    tool_name: str


class MemoryOut(ApiModel):
    """A Memory as exposed over the public API."""

    captured_at: datetime
    content: str
    memory_id: UUID7
    state: MemoryState
    version: PositiveInt


class CreateMemoryBody(ApiModel):
    """Request body for creating a Memory."""

    content: str


class PatchMemoryBody(ApiModel):
    """Request body for patching a Memory."""

    content: str


class PingOut(ApiModel):
    """Internal tool health payload."""

    ok: bool
    tool_name: str


_MEMORIES: dict[UUID, MemoryRecord] = {}


def _to_dto(record: MemoryRecord) -> MemoryOut:
    return MemoryOut(
        captured_at=record.captured_at,
        content=record.content,
        memory_id=record.memory_id,
        state=record.state,
        version=record.version,
    )


async def make_public_ctx() -> PublicRequestCtx:
    """Build the public request context (demo: a fixed user)."""
    return PublicRequestCtx(user_name="demo-user")


async def make_tool_ctx(request: Request) -> ToolRequestCtx:
    """Authenticate the internal tool caller via a static bearer token."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != _TOOL_SECRET:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "not_authenticated",
            "Provide a valid tool bearer token.",
        )
    return ToolRequestCtx(tool_name="pi")


memory_router = ApiRouter(
    prefix="/memories",
    tags=["Memories"],
    security=None,
    ctx_factory=make_public_ctx,
)


@memory_router("GET", "/", status=status.HTTP_200_OK)
async def list_memories(
    _context: PublicRequestCtx,
    *,
    state: QueryParam[MemoryState | None] = None,
) -> list[MemoryOut]:
    """List Memories, optionally filtered by state."""
    records = sorted(_MEMORIES.values(), key=lambda record: record.captured_at)
    if state is not None:
        records = [record for record in records if record.state is state]
    return [_to_dto(record) for record in records]


@memory_router("POST", "/", status=status.HTTP_201_CREATED)
async def create_memory(
    _context: PublicRequestCtx,
    *,
    body: BodyParam[CreateMemoryBody],
) -> MemoryOut:
    """Create a new Memory."""
    record = MemoryRecord(
        captured_at=datetime.now(UTC),
        content=body.content,
        memory_id=uuid7(),
        state=MemoryState.LOOSE,
        version=1,
    )
    _MEMORIES[record.memory_id] = record
    return _to_dto(record)


@memory_router(
    "GET",
    "/{memory_id}",
    status=status.HTTP_200_OK,
    errors=[status.HTTP_404_NOT_FOUND],
)
async def fetch_memory(
    _context: PublicRequestCtx,
    *,
    memory_id: PathParam[UUID],
) -> MemoryOut:
    """Fetch one Memory by id."""
    record = _MEMORIES.get(memory_id)
    if record is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "memory_not_found",
            "Memory not found.",
            details={"memory_id": str(memory_id)},
        )
    return _to_dto(record)


@memory_router(
    "PATCH",
    "/{memory_id}",
    status=status.HTTP_200_OK,
    errors=[status.HTTP_404_NOT_FOUND],
)
async def patch_memory(
    _context: PublicRequestCtx,
    *,
    memory_id: PathParam[UUID],
    body: BodyParam[PatchMemoryBody],
) -> MemoryOut:
    """Patch one Memory's content."""
    record = _MEMORIES.get(memory_id)
    if record is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "memory_not_found",
            "Memory not found.",
            details={"memory_id": str(memory_id)},
        )
    updated = replace(record, content=body.content, version=record.version + 1)
    _MEMORIES[memory_id] = updated
    return _to_dto(updated)


@memory_router(
    "DELETE",
    "/{memory_id}",
    status=status.HTTP_204_NO_CONTENT,
    errors=[status.HTTP_404_NOT_FOUND],
)
async def delete_memory(
    _context: PublicRequestCtx,
    *,
    memory_id: PathParam[UUID],
) -> None:
    """Delete one Memory."""
    if _MEMORIES.pop(memory_id, None) is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "memory_not_found",
            "Memory not found.",
            details={"memory_id": str(memory_id)},
        )


tool_router = ApiRouter(
    prefix="/tools",
    tags=["Tools"],
    security="tool_secret",
    auth_errors=[status.HTTP_401_UNAUTHORIZED],
    ctx_factory=make_tool_ctx,
)


@tool_router("POST", "/ping", status=status.HTTP_200_OK)
async def ping_tool(context: ToolRequestCtx) -> PingOut:
    """Ping the internal tool surface."""
    return PingOut(ok=True, tool_name=context.tool_name)


mount = ApiMount(
    routes={
        "/api": [memory_router],
        "/internal": [tool_router],
    },
)

app = Starlette(
    # TODO: put title and version in the ApiMount
    routes=mount.build_routes(title="Tether API", version="0.1.0"),
)
