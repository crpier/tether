"""Authorization, invocation, tracing, and envelopes for internal tools."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, cast

import structlog
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response

from tether.agent_trace import AgentTraceRecorder
from tether.bucket_item_store import BucketItemProvenance
from tether.capability_contracts import (
    CapabilityOutcome,
    ErrorRule,
    ToolErrorCode,
    catchable_exceptions,
    match_rule,
)
from tether.memory_store import MemoryProvenance
from tether.youtube import CacheMeta
from tether.youtube_quota import QuotaMeta

TOOL_AUTH_HEADER = "X-Tether-Tool-Secret"
"""Header carrying the per-process credential injected into pi at spawn."""


class SessionRegistry:
    """The process-owned set of pi session identities allowed to call tools."""

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def register(self, session_id: str) -> None:
        """Record one live pi session identity."""
        self._sessions.add(session_id)

    def discard(self, session_id: str) -> None:
        """Forget a pi session after its process closes."""
        self._sessions.discard(session_id)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions


class ToolError(BaseModel):
    """Expected failure detail in a `success:false` tool envelope."""

    code: ToolErrorCode
    message: str


class ToolEnvelope(BaseModel):
    """The uniform success or expected-failure shape returned by every tool."""

    success: bool
    result: Any = None
    error: ToolError | None = None
    provenance: MemoryProvenance | BucketItemProvenance | None = None
    quota: QuotaMeta | None = None
    cache: CacheMeta | None = None


class _ToolRuntime(Protocol):
    """Host dependencies required by the internal tool invocation boundary."""

    session_registry: SessionRegistry
    tool_secret: str
    trace_recorder: AgentTraceRecorder


def _runtime(request: Request) -> _ToolRuntime:
    """Read internal-tool dependencies from the canonical host runtime."""
    return cast("_ToolRuntime", request.app.state.runtime)


def _failure(code: ToolErrorCode, message: str) -> ToolEnvelope:
    """Envelope an expected failure without exposing partial result state."""
    return ToolEnvelope(success=False, error=ToolError(code=code, message=message))


def _success(outcome: CapabilityOutcome) -> ToolEnvelope:
    """Envelope a successful capability result and its optional metadata."""
    return ToolEnvelope(
        success=True,
        result=outcome.result,
        provenance=outcome.provenance,
        quota=outcome.quota,
        cache=outcome.cache,
    )


def _validation_message(error: ValidationError) -> str:
    """Render the first input problem as a concise tool-facing message."""
    first = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "(body)"
    return f"{location}: {first['msg']}"


def _envelope_response(envelope: ToolEnvelope) -> JSONResponse:
    """Serialize an envelope under the always-200 tool outcome contract."""
    return JSONResponse(envelope.model_dump(mode="json"))


def _elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed from one monotonic counter reading."""
    return round((perf_counter() - started) * 1000, 3)


class ToolRoute(Route):
    """Starlette route mounting one internal tool endpoint."""

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
    """Authorize, validate, invoke, trace, and envelope one tool capability."""

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
        """Execute the complete internal-tool boundary contract."""
        secret_failure = self._reject_invalid_secret(request)
        if secret_failure is not None:
            return secret_failure
        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        session_failure = self._reject_unknown_session(request, body)
        if session_failure is not None:
            return session_failure
        session_id = cast("str", body["session_id"])
        request.state.session_id = session_id
        with structlog.contextvars.bound_contextvars(
            **self._run_context(request, session_id)
        ):
            envelope, duration_ms = await self._invoke(request, body)
        self._record_tool_call(request, body, envelope, duration_ms)
        return _envelope_response(envelope)

    async def _invoke(
        self,
        request: Request,
        body: dict[str, Any],
    ) -> tuple[ToolEnvelope, float]:
        """Validate and invoke while timing both success and validation failure."""
        started = perf_counter()
        params = self._validated_params(body)
        if isinstance(params, ToolEnvelope):
            return params, _elapsed_ms(started)
        return await self._run_handler(request, params), _elapsed_ms(started)

    def _run_context(self, request: Request, session_id: str) -> dict[str, str]:
        """Return active trace context for structured logs, when available."""
        run = _runtime(request).trace_recorder.current_run(session_id)
        return {} if run is None else {"run_id": run.run_id}

    def _record_tool_call(
        self,
        request: Request,
        body: dict[str, Any],
        envelope: ToolEnvelope,
        duration_ms: float,
    ) -> None:
        """Append one tool call to its session's active trace run."""
        _runtime(request).trace_recorder.record_tool_call(
            session_id=cast("str", body["session_id"]),
            tool=request.url.path.rsplit("/", 1)[-1],
            args={key: value for key, value in body.items() if key != "session_id"},
            envelope=envelope.model_dump(mode="json"),
            duration_ms=duration_ms,
        )

    def _reject_invalid_secret(self, request: Request) -> JSONResponse | None:
        offered_secret = request.headers.get(TOOL_AUTH_HEADER, "")
        if hmac.compare_digest(offered_secret, _runtime(request).tool_secret):
            return None
        return JSONResponse({"detail": "invalid tool secret"}, status_code=401)

    async def _read_body(self, request: Request) -> dict[str, Any] | JSONResponse:
        try:
            body_json: object = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"detail": "request body is not valid JSON"},
                status_code=400,
            )
        if isinstance(body_json, dict):
            return cast("dict[str, Any]", body_json)
        return JSONResponse(
            {"detail": "request body must be a JSON object"},
            status_code=400,
        )

    def _reject_unknown_session(
        self,
        request: Request,
        body: dict[str, Any],
    ) -> JSONResponse | None:
        session_id = body.get("session_id")
        if (
            isinstance(session_id, str)
            and session_id in _runtime(request).session_registry
        ):
            return None
        return JSONResponse({"detail": "unknown session"}, status_code=401)

    def _validated_params(self, body: dict[str, Any]) -> BaseModel | ToolEnvelope:
        try:
            return self.params_model.model_validate(
                {key: value for key, value in body.items() if key != "session_id"}
            )
        except ValidationError as error:
            return _failure("invalid_input", _validation_message(error))

    async def _run_handler(self, request: Request, params: BaseModel) -> ToolEnvelope:
        """Translate only declared domain failures into tool error values."""
        try:
            outcome = await self.handler(request, params)
        except self._catchable as error:
            rule = match_rule(self.errors, error)
            message = "not found" if rule.code == "not_found" else str(error)
            return _failure(rule.code, message)
        return _success(outcome)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Single source of truth for one tool's name, params, handler, and errors."""

    name: str
    params_model: type[BaseModel]
    handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]]
    errors: tuple[ErrorRule, ...] = ()

    @property
    def endpoint(self) -> str:
        """Return this tool's fixed loopback endpoint."""
        return f"/internal/tools/{self.name}"

    def route(self) -> ToolRoute:
        """Mount this spec as a POST endpoint."""
        return ToolRoute(
            self.endpoint,
            ToolEndpoint(self.params_model, self.handler, errors=self.errors),
            methods=["POST"],
        )


__all__ = [
    "TOOL_AUTH_HEADER",
    "SessionRegistry",
    "ToolEndpoint",
    "ToolEnvelope",
    "ToolError",
    "ToolRoute",
    "ToolSpec",
]
