"""OpenTelemetry setup for the host process.

Tracing is always configured: spans are emitted unconditionally. Whether they
leave the process is the only knob — `TelemetryExporter.NONE` drops them, so an
environment that does not want to capture traces simply exports nowhere.

```python
settings = TelemetrySettings()
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
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
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
    settings = TelemetrySettings(exporter=TelemetryExporter.NONE)
    assert settings.exporter is TelemetryExporter.NONE
    ```
    """

    environment: str = "development"
    exporter: TelemetryExporter = TelemetryExporter.NONE
    install_global_provider: bool = True
    service_name: str = "tether-host"
    service_version: str = "0.1.0"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Configured tracing resources for application wiring."""

    tracer: Tracer
    tracer_provider: TracerProvider

    def shutdown(self) -> None:
        """Flush and close telemetry resources."""
        self.tracer_provider.shutdown()


def configure_telemetry(settings: TelemetrySettings) -> Telemetry:
    """Configure OpenTelemetry tracing without vendor-specific exporters.

    Tracing is always on; `TelemetryExporter.NONE` only skips wiring an exporter,
    so spans are still created but go nowhere. Host startup installs the provider
    globally so library code that uses OpenTelemetry directly joins the trace.
    """
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
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    if settings.install_global_provider:
        trace.set_tracer_provider(tracer_provider)
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
