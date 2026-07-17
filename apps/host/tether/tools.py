"""The loopback internal tool surface and its uniform response envelope.

This is the seam a pi process calls back into: a set of `/internal/tools/*`
endpoints, distinct from the public `/memories` REST surface and absent from the
public OpenAPI document. It mounts the same capability executes the REST routes
derive from (`tether.memory_capabilities` for the Memory belt) but presents
them as tools a weak model can call.

Two guarantees shape every handler:

* **Authorization is a gate, not a tool outcome.** A call must carry the
  per-process secret (a header) and a `session_id` that resolves against the
  host `SessionRegistry`; failing either is a hard `401`, never an envelope.
  The surface is therefore unreachable from the public API.
* **Past the gate, every response is the envelope.** `success`/`result`/
  `error`/`provenance`/`quota`, success and error paths alike. Malformed tool
  params (a non-UUID id, blank content) yield a well-formed `success:false`
  envelope and touch no state — a dumb model can never be destructive. Domain
  failures translate through the capability's `ErrorRule` table onto envelope
  codes — the same table the REST surface maps onto status codes. `quota` is
  always null here; `provenance` carries a single Memory's provenance, null
  for collections.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, cast

import structlog
from pydantic import UUID7, BaseModel, PositiveInt, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response

from tether.agent_trace import AgentTraceRecorder
from tether.bucket_items import BucketItemProvenance
from tether.capabilities import (
    CapabilityOutcome,
    ErrorRule,
    ToolErrorCode,
    bind_params,
    catchable_exceptions,
    match_rule,
)
from tether.logging import get_request_logger
from tether.memories import MemoryProvenance, MemoryState
from tether.memory_capabilities import MEMORY_ERRORS, MemoryContent
from tether.memory_capabilities import (
    browse as browse_memories,
)
from tether.memory_capabilities import (
    capture as capture_memory,
)
from tether.memory_capabilities import (
    edit as edit_memory,
)
from tether.memory_capabilities import (
    reject as reject_memory,
)
from tether.memory_capabilities import (
    search as search_memories,
)
from tether.memory_capabilities import (
    tether as tether_memory,
)
from tether.youtube import CacheMeta, QuotaMeta

TOOL_AUTH_HEADER = "X-Tether-Tool-Secret"
"""Header carrying the per-process credential injected into pi at spawn."""


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
    limit: PositiveInt = 50


class SearchParams(BaseModel):
    """Params for the assistant's keyword Search over tethered Memories."""

    q: str
    limit: PositiveInt = 50


class ReviewDigestParams(BaseModel):
    """Params for the AI-assisted Review digest.

    The digest is computed over the whole live queue, so it takes no inputs
    beyond the session identity the gate already requires.
    """


def _fail(code: ToolErrorCode, message: str) -> ToolEnvelope:
    """Envelope a failure, leaving `result` null so no state leaks out."""
    return ToolEnvelope(success=False, error=ToolError(code=code, message=message))


def _success(outcome: CapabilityOutcome) -> ToolEnvelope:
    """Envelope a capability outcome, carrying its metadata alongside."""
    return ToolEnvelope(
        success=True,
        result=outcome.result,
        provenance=outcome.provenance,
        quota=outcome.quota,
        cache=outcome.cache,
    )


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

    The wrapper owns the cross-cutting contract so each capability execute
    stays a thin call into its service: it enforces the secret + session gate
    (hard `401`), validates the params model into a `success:false` envelope on
    failure, translates the capability's `ErrorRule` table onto envelope error
    codes, and wraps the returned `CapabilityOutcome` in the envelope.
    """

    def __init__(
        self,
        params_model: type[BaseModel],
        handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]],
        *,
        errors: tuple[ErrorRule, ...] = (),
    ) -> None:
        self.params_model: type[BaseModel] = params_model
        self.handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]] = handler
        self.errors: tuple[ErrorRule, ...] = errors
        self._catchable: tuple[type[Exception], ...] = catchable_exceptions(errors)

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
        # `_reject_unknown_session` already proved this is a registered `str`.
        session_id = cast("str", body["session_id"])
        # Handlers only ever see `request`/validated params (`session_id` is
        # stripped before params validation); a capability that needs the
        # caller's identity — e.g. resolving which conversation a pi session
        # belongs to — reads it back off `request.state`.
        request.state.session_id = session_id
        run_context = self._run_context(request, session_id)
        with structlog.contextvars.bound_contextvars(**run_context):
            envelope, duration_ms = await self._invoke(request, body)
        self._record_tool_call(request, body, envelope, duration_ms)
        return _envelope_response(envelope)

    async def _invoke(
        self, request: Request, body: dict[str, Any]
    ) -> tuple[ToolEnvelope, float]:
        """Validate params and run the handler, timing the tool call.

        A validation failure short-circuits to a `success:false` envelope without
        touching the handler; both paths are timed so the trace records how long
        every tool call took, malformed input included.
        """
        started = perf_counter()
        params = self._validated_params(body)
        if isinstance(params, ToolEnvelope):
            return params, _elapsed_ms(started)
        return await self._run_handler(request, params), _elapsed_ms(started)

    def _run_context(self, request: Request, session_id: str) -> dict[str, str]:
        """Bind the active run id so handler logs correlate with the trace."""
        recorder = _trace_recorder(request)
        if recorder is None:
            return {}
        run = recorder.current_run(session_id)
        return {} if run is None else {"run_id": run.run_id}

    def _record_tool_call(
        self,
        request: Request,
        body: dict[str, Any],
        envelope: ToolEnvelope,
        duration_ms: float,
    ) -> None:
        """Append this tool call to its session's active run, if recording."""
        recorder = _trace_recorder(request)
        if recorder is None:
            return
        recorder.record_tool_call(
            # Reached only after `__call__` validated the session id is a `str`.
            session_id=cast("str", body["session_id"]),
            tool=request.url.path.rsplit("/", 1)[-1],
            args={key: value for key, value in body.items() if key != "session_id"},
            envelope=envelope.model_dump(mode="json"),
            duration_ms=duration_ms,
        )

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

    def _validated_params(self, body: dict[str, Any]) -> BaseModel | ToolEnvelope:
        payload: dict[str, Any] = {
            key: value for key, value in body.items() if key != "session_id"
        }
        try:
            return self.params_model.model_validate(payload)
        except ValidationError as error:
            return _fail("invalid_input", _validation_message(error))

    async def _run_handler(self, request: Request, params: BaseModel) -> ToolEnvelope:
        """Run the capability, translating its domain failures onto envelope codes.

        Absence is always the flat "not found" message so no identifier detail
        leaks to the model; other codes carry the exception's own message.
        """
        try:
            outcome = await self.handler(request, params)
        except self._catchable as error:
            rule = match_rule(self.errors, error)
            message = "not found" if rule.code == "not_found" else str(error)
            return _fail(rule.code, message)
        return _success(outcome)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One internal tool: its name, params model, handler, and error table.

    The single source of truth for a tool. `route()` mounts it as a POST
    endpoint and `tether.tool_schemas` derives its codegen schema from the same
    spec, so a tool's name/endpoint/params can never drift between the mounted
    surface and the generated shim. The endpoint is always
    `/internal/tools/{name}`.

    >>> class PokeParams(BaseModel):
    ...     pass
    >>> async def poke(request: Request, params: PokeParams) -> CapabilityOutcome:
    ...     return CapabilityOutcome(result=None)
    >>> ToolSpec("poke", PokeParams, poke).endpoint
    '/internal/tools/poke'
    """

    name: str
    params_model: type[BaseModel]
    handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]]
    errors: tuple[ErrorRule, ...] = ()

    @property
    def endpoint(self) -> str:
        """The loopback path this tool mounts at."""
        return f"/internal/tools/{self.name}"

    def route(self) -> ToolRoute:
        """Mount this spec as its POST `/internal/tools/*` endpoint."""
        return ToolRoute(
            self.endpoint,
            ToolEndpoint(self.params_model, self.handler, errors=self.errors),
            methods=["POST"],
        )


def _elapsed_ms(started: float) -> float:
    """Milliseconds elapsed since a `perf_counter` reading."""
    return round((perf_counter() - started) * 1000, 3)


def _trace_recorder(request: Request) -> AgentTraceRecorder | None:
    """Return the host's agent-trace recorder, if one is installed.

    `getattr` with a `None` default keeps the tool path working in setups (some
    tests) that never install a recorder onto `app.state`.
    """
    return getattr(request.app.state, "trace_recorder", None)


async def _review_digest(request: Request) -> CapabilityOutcome:
    """Compute the read-only AI-assisted Review digest over the live queue."""
    digest = await request.app.state.review_service.review_digest(
        logger=get_request_logger(request)
    )
    return CapabilityOutcome(result=digest.model_dump(mode="json"))


MEMORY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("capture", CaptureParams, bind_params(capture_memory), MEMORY_ERRORS),
    ToolSpec("browse", BrowseParams, bind_params(browse_memories), MEMORY_ERRORS),
    ToolSpec("search", SearchParams, bind_params(search_memories), MEMORY_ERRORS),
    ToolSpec("review_digest", ReviewDigestParams, bind_params(_review_digest)),
    ToolSpec("tether", TetherParams, bind_params(tether_memory), MEMORY_ERRORS),
    ToolSpec("edit", EditParams, bind_params(edit_memory), MEMORY_ERRORS),
    ToolSpec("reject", RejectParams, bind_params(reject_memory), MEMORY_ERRORS),
)
"""The Memory capabilities exposed as internal tools, in generated-file order."""


def internal_tool_routes() -> list[Route]:
    """Mount the Memory capabilities as `/internal/tools/*` POST endpoints.

    These are returned separately from the public routes so they can be mounted
    on the app without being handed to `openapi_routes` — the tool surface is
    deliberately absent from the public OpenAPI document and generated client.
    """
    return [spec.route() for spec in MEMORY_TOOL_SPECS]
