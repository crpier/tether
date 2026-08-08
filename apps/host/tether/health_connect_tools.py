"""Read-only Health Connect Telemetry tools for the closed agent world."""

from __future__ import annotations

from datetime import timedelta
from json import dumps
from typing import Any, Self, cast

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import CapabilityOutcome, bind_params
from tether.health_connect import (
    HealthConnectRecordRead,
    HealthConnectService,
    HealthRecordType,
)
from tether.tools import ToolSpec

_HEALTH_RECORD_DATA_LIMIT_BYTES = 4 * 1_024
"""Maximum raw data retained for one queried Health Connect record."""


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

    @model_validator(mode="after")
    def ordered_time_window(self) -> Self:
        """Reject reversed windows before any Telemetry read occurs."""
        if self.after > self.before:
            raise HealthConnectQueryRangeError
        if self.before - self.after > timedelta(days=31):
            raise HealthConnectSummaryRangeError
        return self


class QueryHealthConnectParams(BaseModel):
    """Inspect a few individual Health Connect records; use summary for overviews."""

    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    limit: int = Field(default=5, ge=1, le=10)
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


def _bounded_record_result(record: HealthConnectRecordRead) -> dict[str, Any]:
    """Keep raw reflected data from injecting unbounded agent context."""
    record_result = record.model_dump(mode="json")
    record_data = cast("dict[str, object]", record_result["data"])
    data_size_bytes = len(
        dumps(record_data, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if data_size_bytes > _HEALTH_RECORD_DATA_LIMIT_BYTES:
        record_result["data"] = {
            "original_size_bytes": data_size_bytes,
            "truncated": True,
        }
    return record_result


async def _health_connect_inventory(request: Request) -> CapabilityOutcome:
    """Read current projection metadata without exposing append-only history."""
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    entries = await service.inventory()
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


async def _summarize_health_connect(
    request: Request, params: SummarizeHealthConnectParams
) -> CapabilityOutcome:
    """Return compact current metrics for overview and trend requests."""
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    summary = await service.summarize_current(after=params.after, before=params.before)
    return CapabilityOutcome(result=summary.model_dump(mode="json"))


async def _query_health_connect(
    request: Request, params: QueryHealthConnectParams
) -> CapabilityOutcome:
    """Return bounded records from the latest non-tombstoned projections."""
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    if params.record_type == "exercise":
        records = await service.query_current_exercise(
            after=params.after,
            before=params.before,
            limit=params.limit,
        )
    elif params.record_type == "heart_rate":
        records = await service.query_current_heart_rates(
            after=params.after,
            before=params.before,
            limit=params.limit,
        )
    elif params.record_type == "sleep":
        records = await service.query_current_sleep(
            after=params.after,
            before=params.before,
            limit=params.limit,
        )
    elif params.record_type == "steps":
        records = await service.query_current_steps(
            after=params.after,
            before=params.before,
            limit=params.limit,
        )
    else:
        records = await service.query_current_generic(
            record_type=params.record_type,
            after=params.after,
            before=params.before,
            limit=params.limit,
        )
    return CapabilityOutcome(
        result=[_bounded_record_result(record) for record in records]
    )


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
