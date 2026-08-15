"""Read-only Health Connect Telemetry tools for the closed agent world."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from starlette.requests import Request
from starlette.routing import Route

from tether.app_runtime import app_runtime
from tether.capabilities import CapabilityOutcome, bind_params
from tether.health_connect import HealthRecordType
from tether.tools import ToolSpec


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
    telemetry = app_runtime(request.app).health_connect_telemetry
    entries = await telemetry.fetch_inventory()
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


async def _summarize_health_connect(
    request: Request, params: SummarizeHealthConnectParams
) -> CapabilityOutcome:
    """Return compact current metrics for overview and trend requests."""
    telemetry = app_runtime(request.app).health_connect_telemetry
    summary = await telemetry.fetch_summary(
        after=params.after, before=params.before, bucket=params.bucket
    )
    return CapabilityOutcome(result=summary.model_dump(mode="json"))


async def _query_health_connect(
    request: Request, params: QueryHealthConnectParams
) -> CapabilityOutcome:
    """Return bounded records from the latest non-tombstoned projections."""
    telemetry = app_runtime(request.app).health_connect_telemetry
    current = await telemetry.fetch_records(
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
