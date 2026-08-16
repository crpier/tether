"""HTTP server-span ownership over complete request dispatch."""

from typing import Any, Protocol, cast

from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from tether.telemetry_model import Telemetry


class _TelemetryRuntime(Protocol):
    """Tracing dependency available while the host serves requests."""

    telemetry: Telemetry


def _runtime(request: Request) -> _TelemetryRuntime:
    """Read tracing from the canonical host runtime."""
    return cast("_TelemetryRuntime", request.app.state.runtime)


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Create and settle one server span for each HTTP request."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Record request attributes, response status, and unexpected defects."""
        with _runtime(request).telemetry.tracer.start_as_current_span(
            f"HTTP {request.method} {request.url.path}",
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": request.method,
                "url.path": request.url.path,
            },
        ) as span:
            try:
                response = await call_next(request)
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            return response
