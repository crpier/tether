"""The loopback internal tool surface and its uniform response envelope.

This is the seam a pi process calls back into: a set of `/internal/tools/*`
endpoints, distinct from the public `/memories` REST surface and absent from the
public OpenAPI document. It wires the same `MemoryService` capabilities the REST
routes use — capture, browse, search, tether, edit, reject — but presents them
as tools a weak model can call.

Two guarantees shape every handler:

* **Authorization is a gate, not a tool outcome.** A call must carry the
  per-process secret (a header) and a `session_id` that resolves against the
  host `SessionRegistry`; failing either is a hard `401`, never an envelope.
  The surface is therefore unreachable from the public API.
* **Past the gate, every response is the envelope.** `success`/`result`/
  `error`/`provenance`/`quota`, success and error paths alike. Malformed tool
  params (a non-UUID id, blank content) yield a well-formed `success:false`
  envelope and touch no state — a dumb model can never be destructive. `quota`
  is always null here; `provenance` carries a single Memory's provenance, null
  for collections.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response

from tether.bucket_items import (
    BucketItemConflictError,
    BucketItemNotFoundError,
    BucketItemProvenance,
    EmptyBucketSearchQueryError,
    EmptyIntentContextError,
    InvalidItemDataError,
)
from tether.logging import Logger, get_request_logger
from tether.memories import (
    EmptySearchQueryError,
    Fetched,
    Memory,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryProvenance,
    MemoryState,
)
from tether.routes import MemoryContent, MemoryRead
from tether.triggers import (
    InvalidTriggerSpecError,
    TriggerConflictError,
    TriggerNotFoundError,
)
from tether.youtube import (
    CacheMeta,
    EmptyYouTubeSearchQueryError,
    QuotaMeta,
    TranscriptUnavailableError,
    YouTubeQuotaExceededError,
    YouTubeVideoNotFoundError,
)

TOOL_AUTH_HEADER = "X-Tether-Tool-Secret"
"""Header carrying the per-process credential injected into pi at spawn."""

type ToolErrorCode = Literal["invalid_input", "not_found", "conflict", "quota_exceeded"]


class SessionRegistry:
    """The host's record of which pi session ids currently exist.

    pi owns session lifecycle; the host only tracks which sessions it has
    spawned so the tool surface can resolve identity. Until the pi runtime
    lands, sessions are registered directly (e.g. by tests).
    """

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def register(self, session_id: str) -> None:
        """Record a session id as a valid caller identity."""
        self._sessions.add(session_id)

    def discard(self, session_id: str) -> None:
        """Forget a session id once its pi process is gone."""
        self._sessions.discard(session_id)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions


class ToolError(BaseModel):
    """The failure detail in a `success:false` envelope.

    >>> ToolError(code="not_found", message="memory not found").code
    'not_found'
    """

    code: ToolErrorCode
    message: str


class ToolEnvelope(BaseModel):
    """The uniform shape every tool returns.

    `provenance` carries a single Memory's provenance where applicable and is
    null for collections. `quota` and `cache` are populated only by tools that
    front an external, quota-metered API (YouTube ingestion): `quota` reports the
    remaining budget after a guarded call and `cache` whether the result was
    served live or from cache. Both stay null for the Memory and Bucket tools.

    >>> ToolEnvelope(success=True, result={"id": "x"}).quota is None
    True
    """

    success: bool
    result: Any = None
    error: ToolError | None = None
    provenance: MemoryProvenance | BucketItemProvenance | None = None
    quota: QuotaMeta | None = None
    cache: CacheMeta | None = None


class CaptureParams(BaseModel):
    """Params for capturing a loose Memory."""

    content: MemoryContent


class TetherParams(BaseModel):
    """Params for promoting a loose Memory to tethered."""

    memory_id: UUID7
    version: PositiveInt


class EditParams(BaseModel):
    """Params for editing a Memory's content at an observed version."""

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


class SearchParams(BaseModel):
    """Params for the assistant's keyword Search over tethered Memories."""

    q: str
    limit: PositiveInt = 50


class ReviewDigestParams(BaseModel):
    """Params for the AI-assisted Review digest.

    The digest is computed over the whole live queue, so it takes no inputs
    beyond the session identity the gate already requires.
    """


def _memory_reference(memory_id: UUID, version: PositiveInt) -> Memory[Fetched]:
    """Build a detached Memory carrying only the identity a mutation acts on.

    The service's tether/edit/delete read just `id` and `version` to run their
    optimistic-concurrency check and re-fetch the live row, so a hand-built
    reference suffices; `content` is a required column with no role here.
    """
    return cast(
        "Memory[Fetched]",
        Memory.construct(content="", id=memory_id, version=version),
    )


def _ok_memory(memory: Memory[Fetched]) -> ToolEnvelope:
    """Envelope a single-Memory result, surfacing its provenance."""
    return ToolEnvelope(
        success=True,
        result=MemoryRead.from_memory(memory).model_dump(mode="json"),
        provenance=memory.provenance,
    )


def _ok_memories(memories: list[Memory[Fetched]]) -> ToolEnvelope:
    """Envelope a Memory collection; provenance is null for collections."""
    return ToolEnvelope(
        success=True,
        result=[
            MemoryRead.from_memory(memory).model_dump(mode="json")
            for memory in memories
        ],
    )


def _fail(code: ToolErrorCode, message: str) -> ToolEnvelope:
    """Envelope a failure, leaving `result` null so no state leaks out."""
    return ToolEnvelope(success=False, error=ToolError(code=code, message=message))


def _validation_message(error: ValidationError) -> str:
    """Render the first validation problem as a short, model-free message."""
    first = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "(body)"
    return f"{location}: {first['msg']}"


def _envelope_response(envelope: ToolEnvelope) -> JSONResponse:
    """Serialise an envelope; tool outcomes are always HTTP 200."""
    return JSONResponse(envelope.model_dump(mode="json"))


class ToolRoute(Route):
    """A route that mounts a tool endpoint under Starlette's request contract."""

    def __init__(
        self,
        path: str,
        endpoint: ToolEndpoint,
        *,
        methods: list[str] | None = None,
    ) -> None:
        super().__init__(path, endpoint, methods=methods)
        self.app = request_response(endpoint)


class ToolEndpoint:
    """An authorised, envelope-wrapped handler for one tool capability.

    The wrapper owns the cross-cutting contract so each tool body stays a thin
    call into `MemoryService`: it enforces the secret + session gate (hard
    `401`), validates the params model into a `success:false` envelope on
    failure, and translates Memory domain errors onto envelope error codes.
    """

    def __init__(
        self,
        params_model: type[BaseModel],
        handler: Callable[[Request, Any], Awaitable[ToolEnvelope]],
    ) -> None:
        self.params_model: type[BaseModel] = params_model
        self.handler: Callable[[Request, Any], Awaitable[ToolEnvelope]] = handler

    async def __call__(self, request: Request) -> Response:
        secret_failure = self._reject_invalid_secret(request)
        if secret_failure is not None:
            return secret_failure
        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        session_failure = self._reject_unknown_session(request, body)
        if session_failure is not None:
            return session_failure
        params = self._validated_params(body)
        if isinstance(params, JSONResponse):
            return params
        return _envelope_response(await self._run_handler(request, params))

    def _reject_invalid_secret(self, request: Request) -> JSONResponse | None:
        offered_secret = request.headers.get(TOOL_AUTH_HEADER, "")
        expected_secret = cast("str", request.app.state.tool_secret)
        if hmac.compare_digest(offered_secret, expected_secret):
            return None
        return JSONResponse({"detail": "invalid tool secret"}, status_code=401)

    async def _read_body(self, request: Request) -> dict[str, Any] | JSONResponse:
        try:
            body_json: object = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"detail": "request body is not valid JSON"}, status_code=400
            )
        if isinstance(body_json, dict):
            return cast("dict[str, Any]", body_json)
        return JSONResponse(
            {"detail": "request body must be a JSON object"}, status_code=400
        )

    def _reject_unknown_session(
        self, request: Request, body: dict[str, Any]
    ) -> JSONResponse | None:
        registry = cast("SessionRegistry", request.app.state.session_registry)
        session_id = body.get("session_id")
        if isinstance(session_id, str) and session_id in registry:
            return None
        return JSONResponse({"detail": "unknown session"}, status_code=401)

    def _validated_params(self, body: dict[str, Any]) -> BaseModel | JSONResponse:
        payload: dict[str, Any] = {
            key: value for key, value in body.items() if key != "session_id"
        }
        try:
            return self.params_model.model_validate(payload)
        except ValidationError as error:
            return _envelope_response(
                _fail("invalid_input", _validation_message(error))
            )

    async def _run_handler(self, request: Request, params: BaseModel) -> ToolEnvelope:
        try:
            return await self.handler(request, params)
        except (
            MemoryNotFoundError,
            BucketItemNotFoundError,
            TriggerNotFoundError,
            YouTubeVideoNotFoundError,
            TranscriptUnavailableError,
        ):
            return _fail("not_found", "not found")
        except (
            MemoryConflictError,
            BucketItemConflictError,
            TriggerConflictError,
        ) as error:
            return _fail("conflict", str(error))
        except YouTubeQuotaExceededError as error:
            return _fail("quota_exceeded", str(error))
        except (
            EmptySearchQueryError,
            EmptyBucketSearchQueryError,
            EmptyIntentContextError,
            InvalidItemDataError,
            InvalidTriggerSpecError,
            EmptyYouTubeSearchQueryError,
        ) as error:
            return _fail("invalid_input", str(error))


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


async def _capture(request: Request, params: CaptureParams) -> ToolEnvelope:
    """Capture a loose Memory."""
    memory = await request.app.state.memory_service.capture(
        params.content, logger=_tool_logger(request)
    )
    return _ok_memory(memory)


async def _browse(request: Request, params: BrowseParams) -> ToolEnvelope:
    """Filter the review queue (`loose`) or browse the corpus (`tethered`)."""
    memories = await request.app.state.memory_service.browse_by_state(
        params.state, logger=_tool_logger(request)
    )
    return _ok_memories(memories)


async def _search(request: Request, params: SearchParams) -> ToolEnvelope:
    """Keyword Search over tethered Memories."""
    memories = await request.app.state.memory_service.search(
        params.q, limit=params.limit, logger=_tool_logger(request)
    )
    return _ok_memories(memories)


async def _review_digest(request: Request, _params: ReviewDigestParams) -> ToolEnvelope:
    """Compute the read-only AI-assisted Review digest over the live queue."""
    digest = await request.app.state.review_service.review_digest(
        logger=_tool_logger(request)
    )
    return ToolEnvelope(success=True, result=digest.model_dump(mode="json"))


async def _tether(request: Request, params: TetherParams) -> ToolEnvelope:
    """Promote a loose Memory to tethered."""
    memory = await request.app.state.memory_service.tether(
        _memory_reference(params.memory_id, params.version),
        logger=_tool_logger(request),
    )
    return _ok_memory(memory)


async def _edit(request: Request, params: EditParams) -> ToolEnvelope:
    """Edit a Memory's content; a human-authored edit keeps trust."""
    memory = await request.app.state.memory_service.edit_content(
        _memory_reference(params.memory_id, params.version),
        params.content,
        logger=_tool_logger(request),
    )
    return _ok_memory(memory)


async def _reject(request: Request, params: RejectParams) -> ToolEnvelope:
    """Soft-delete (reject) a Memory."""
    memory = await request.app.state.memory_service.delete(
        _memory_reference(params.memory_id, params.version),
        logger=_tool_logger(request),
    )
    return _ok_memory(memory)


def internal_tool_routes() -> list[Route]:
    """Mount the six Memory capabilities as `/internal/tools/*` POST endpoints.

    These are returned separately from the public routes so they can be mounted
    on the app without being handed to `openapi_routes` — the tool surface is
    deliberately absent from the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/capture",
            ToolEndpoint(CaptureParams, _capture),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/browse",
            ToolEndpoint(BrowseParams, _browse),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/search",
            ToolEndpoint(SearchParams, _search),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/review_digest",
            ToolEndpoint(ReviewDigestParams, _review_digest),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/tether",
            ToolEndpoint(TetherParams, _tether),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/edit",
            ToolEndpoint(EditParams, _edit),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/reject",
            ToolEndpoint(RejectParams, _reject),
            methods=["POST"],
        ),
    ]
