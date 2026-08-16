"""Configuration and owned resources for OpenTelemetry tracing."""

from dataclasses import dataclass
from enum import Enum

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer


class TelemetryExporter(Enum):
    """Supported trace exporter modes."""

    CONSOLE = "console"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Vendor-neutral tracing settings for one host process."""

    environment: str = "development"
    exporter: TelemetryExporter = TelemetryExporter.NONE
    install_global_provider: bool = True
    service_name: str = "tether-host"
    service_version: str = "0.1.0"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Tracing resources acquired and closed by the host lifecycle."""

    tracer: Tracer
    tracer_provider: TracerProvider

    def shutdown(self) -> None:
        """Flush and close the owned provider exactly once at host shutdown."""
        self.tracer_provider.shutdown()
