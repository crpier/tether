"""Read-only Health Connect Telemetry tools for the closed agent world."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Protocol, Self, cast

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome
from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.telemetry import HealthConnectTelemetry
from tether.tool_runtime import ToolSpec


class _HealthConnectToolsRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    health_connect_telemetry: HealthConnectTelemetry


def _runtime(request: Request) -> _HealthConnectToolsRuntime:
    """Read the installed application runtime off the request."""
    return cast("_HealthConnectToolsRuntime", request.app.state.runtime)


class HealthConnectInventoryParams(BaseModel):
    """List populated Health Connect record types and their UTC time bounds."""


class HealthConnectQueryRangeError(ValueError):
    """The requested time window runs backwards."""

    def __init__(self) -> None:
        super().__init__("after must not be later than before")


class HealthConnectSummaryRangeError(ValueError):
    """The requested summary window exceeds its aggregate-read bound."""

    def __init__(self) -> None:
        super().__init__("summary window must not exceed 31 days")


class SummarizeHealthConnectParams(BaseModel):
    """Use compact Health Connect aggregates for overview and trend requests."""

    after: AwareDatetime
    before: AwareDatetime
    bucket: Literal["none", "day"] = "none"

    @model_validator(mode="after")
    def ordered_time_window(self) -> Self:
        """Reject reversed windows before any Telemetry read occurs."""
        if self.after > self.before:
            raise HealthConnectQueryRangeError
        if self.before - self.after > timedelta(days=31):
            raise HealthConnectSummaryRangeError
        return self


class QueryHealthConnectParams(BaseModel):
    """Inspect individual Health Connect records; use summary for overviews."""

    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    limit: int = Field(default=5, ge=1, le=1_000)
    record_type: HealthRecordType

    @model_validator(mode="after")
    def ordered_time_window(self) -> Self:
        """Reject reversed windows before any Telemetry read occurs."""
        if (
            self.after is not None
            and self.before is not None
            and self.after > self.before
        ):
            raise HealthConnectQueryRangeError
        return self


async def _health_connect_inventory(request: Request) -> CapabilityOutcome:
    """Read current projection metadata without exposing append-only history."""
    telemetry = _runtime(request).health_connect_telemetry
    entries = await telemetry.inventory.fetch_inventory()
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


async def _summarize_health_connect(
    request: Request, params: SummarizeHealthConnectParams
) -> CapabilityOutcome:
    """Return compact current metrics for overview and trend requests."""
    telemetry = _runtime(request).health_connect_telemetry
    summary = await telemetry.summary.fetch_summary(
        after=params.after, before=params.before, bucket=params.bucket
    )
    return CapabilityOutcome(result=summary.model_dump(mode="json"))


async def _query_health_connect(
    request: Request, params: QueryHealthConnectParams
) -> CapabilityOutcome:
    """Return bounded records from the latest non-tombstoned projections."""
    telemetry = _runtime(request).health_connect_telemetry
    current = await telemetry.records.fetch_records(
        record_type=params.record_type,
        after=params.after,
        before=params.before,
        limit=params.limit,
    )
    return CapabilityOutcome(result=current.model_dump(mode="json"))


HEALTH_CONNECT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "health_connect_inventory",
        HealthConnectInventoryParams,
        bind_params(_health_connect_inventory),
    ),
    ToolSpec(
        "query_health_connect",
        QueryHealthConnectParams,
        _query_health_connect,
    ),
    ToolSpec(
        "summarize_health_connect",
        SummarizeHealthConnectParams,
        _summarize_health_connect,
    ),
)
"""Read-only Health Connect capabilities exposed as internal tools."""


def internal_health_connect_tool_routes() -> list[Route]:
    """Mount Health Connect reads under `/internal/tools/*`."""
    return [spec.route() for spec in HEALTH_CONNECT_TOOL_SPECS]
