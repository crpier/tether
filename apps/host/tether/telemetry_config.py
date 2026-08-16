"""OpenTelemetry provider construction for host startup."""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from tether.telemetry_model import Telemetry, TelemetryExporter, TelemetrySettings


def configure_telemetry(settings: TelemetrySettings) -> Telemetry:
    """Acquire tracing resources and optionally install the global provider."""
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
