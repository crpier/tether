"""Health Connect Integration interface (ADR-0025).

The only import surface for Tether code outside this package. Everything else
in ``tether.health_connect`` is an internal seam owned by this Integration and
its tests.
"""

from tether.health_connect.ingestion import HealthConnectIngestion
from tether.health_connect.persistence import (
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
    create_health_connect_schema,
)
from tether.health_connect.routes import router
from tether.health_connect.telemetry import HealthConnectTelemetry
from tether.health_connect.tools import (
    HEALTH_CONNECT_TOOL_SPECS,
    internal_health_connect_tool_routes,
)

__all__ = [
    "HEALTH_CONNECT_TOOL_SPECS",
    "HcExerciseEpisodeSummary",
    "HcSleepEpisodeSummary",
    "HealthConnectIngestion",
    "HealthConnectTelemetry",
    "create_health_connect_schema",
    "internal_health_connect_tool_routes",
    "router",
]
