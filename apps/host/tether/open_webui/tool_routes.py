"""FastAPI routes adapting retained `ToolSpec` capabilities for Open WebUI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from time import perf_counter
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars

from tether.open_webui.tool_specs import selected_tool_specs
from tether.tool_runtime import ToolEnvelope, ToolError, ToolSpec, invoke_tool_spec

_logger = structlog.stdlib.get_logger("tether.open_webui.tools")


def _log_tool_call(operation: str, started: float, *, success: bool) -> None:
    """Emit only bounded tool metadata, excluding ambient request context."""
    request_context = get_contextvars()
    clear_contextvars()
    try:
        _logger.info(
            "Open WebUI tool call",
            operation=operation,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            success=success,
        )
    finally:
        _ = bind_contextvars(**request_context)


def _invalid_input(error: RequestValidationError) -> ToolEnvelope:
    """Translate FastAPI body validation into the established tool envelope."""
    first = error.errors()[0]
    location_parts = first["loc"]
    if location_parts and location_parts[0] == "body":
        location_parts = location_parts[1:]
    location = ".".join(str(part) for part in location_parts) or "(body)"
    return ToolEnvelope(
        success=False,
        error=ToolError(code="invalid_input", message=f"{location}: {first['msg']}"),
    )


class _EnvelopeValidationRoute(APIRoute):
    """Convert framework-level body validation into a tool result value."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def envelope_handler(request: Request) -> Response:
            started = perf_counter()
            try:
                return await original_handler(request)
            except RequestValidationError as error:
                _log_tool_call(self.operation_id or self.name, started, success=False)
                return JSONResponse(_invalid_input(error).model_dump(mode="json"))

        return envelope_handler


def _endpoint(spec: ToolSpec) -> Callable[..., Awaitable[ToolEnvelope]]:
    """Build an annotated endpoint so FastAPI emits the spec's model `$ref`."""

    async def invoke(request: Request, params: BaseModel) -> ToolEnvelope:
        started = perf_counter()
        success = False
        try:
            envelope = await invoke_tool_spec(spec, request, params)
            success = envelope.success
            return envelope
        finally:
            _log_tool_call(spec.name, started, success=success)

    invoke.__name__ = spec.name
    invoke.__annotations__["params"] = spec.params_model
    return invoke


def open_webui_tool_router() -> APIRouter:
    """Build the fixed Open WebUI schema endpoint and selected POST operations."""
    router = APIRouter(route_class=_EnvelopeValidationRoute)
    for spec in selected_tool_specs():
        router.add_api_route(
            f"/tools/{spec.name}",
            _endpoint(spec),
            methods=["POST"],
            description=spec.params_model.__doc__,
            operation_id=spec.name,
            response_model=ToolEnvelope,
        )

    document = get_openapi(
        title="Tether tools",
        version="1.0.0",
        routes=router.routes,
    )
    components = document.setdefault("components", {})
    components["securitySchemes"] = {
        "OpenWebUIBearer": {"type": "http", "scheme": "bearer"}
    }
    for path_item in document["paths"].values():
        path_item["post"]["security"] = [{"OpenWebUIBearer": []}]
    for route in router.routes:
        if isinstance(route, APIRoute):
            route.include_in_schema = False

    async def openapi_document() -> dict[str, Any]:
        """Return the standalone schema consumed by Open WebUI."""
        return document

    router.add_api_route(
        "/tools/openapi.json",
        openapi_document,
        methods=["GET"],
        include_in_schema=False,
    )
    return router


__all__ = ["open_webui_tool_router"]
