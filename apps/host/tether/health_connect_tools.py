"""Read-only Health Connect Telemetry tools for the closed agent world."""

from __future__ import annotations

from typing import Self, cast

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import CapabilityOutcome, bind_params
from tether.health_connect import HealthConnectService, HealthRecordType
from tether.tools import ToolSpec


class HealthConnectInventoryParams(BaseModel):
    """List populated Health Connect record types and their UTC time bounds."""


class HealthConnectQueryRangeError(ValueError):
    """The requested time window runs backwards."""

    def __init__(self) -> None:
        super().__init__("after must not be later than before")


class QueryHealthConnectParams(BaseModel):
    """Read current Health Connect records in an optional aware UTC time window."""

    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
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
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    entries = await service.inventory()
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


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
        result=[record.model_dump(mode="json") for record in records]
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
)
"""Read-only Health Connect capabilities exposed as internal tools."""


def internal_health_connect_tool_routes() -> list[Route]:
    """Mount Health Connect reads under `/internal/tools/*`."""
    return [spec.route() for spec in HEALTH_CONNECT_TOOL_SPECS]
