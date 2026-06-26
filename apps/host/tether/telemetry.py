"""OpenTelemetry setup for the host process.

```python
settings = TelemetrySettings(enabled=True)
telemetry = configure_telemetry(settings)
```
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class TelemetryExporter(Enum):
    """Supported trace exporter modes."""

    CONSOLE = "console"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Vendor-neutral OpenTelemetry settings.

    ```python
    settings = TelemetrySettings(enabled=True, exporter=TelemetryExporter.NONE)
    assert settings.enabled
    ```
    """

    enabled: bool = False
    environment: str = "development"
    exporter: TelemetryExporter = TelemetryExporter.CONSOLE
    service_name: str = "tether-host"
    service_version: str = "0.1.0"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Configured tracing resources for application wiring."""

    tracer: Tracer
    tracer_provider: TracerProvider | None = None

    def shutdown(self) -> None:
        """Flush and close telemetry resources."""
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


def configure_telemetry(settings: TelemetrySettings) -> Telemetry:
    """Configure OpenTelemetry tracing without vendor-specific exporters."""
    if not settings.enabled:
        return Telemetry(tracer=trace.NoOpTracerProvider().get_tracer("tether"))

    tracer_provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.service_version,
                "deployment.environment.name": settings.environment,
            }
        )
    )
    if settings.exporter is TelemetryExporter.CONSOLE:
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
    return Telemetry(
        tracer=tracer_provider.get_tracer("tether"),
        tracer_provider=tracer_provider,
    )


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Create a server span for each HTTP request."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        telemetry = cast("Telemetry", request.app.state.telemetry)
        with telemetry.tracer.start_as_current_span(
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


__all__ = [
    "Telemetry",
    "TelemetryExporter",
    "TelemetryMiddleware",
    "TelemetrySettings",
    "configure_telemetry",
]
