"""Compatibility exports for focused OpenTelemetry ownership modules."""

from tether.telemetry_config import configure_telemetry
from tether.telemetry_middleware import TelemetryMiddleware
from tether.telemetry_model import (
    Telemetry,
    TelemetryExporter,
    TelemetrySettings,
)

__all__ = [
    "Telemetry",
    "TelemetryExporter",
    "TelemetryMiddleware",
    "TelemetrySettings",
    "configure_telemetry",
]
