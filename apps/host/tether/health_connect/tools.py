"""Health Telemetry reads and Evidence-backed Health plan tools."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import (
    UUID7,
    AwareDatetime,
    BaseModel,
    Field,
    PositiveInt,
    model_validator,
)
from starlette.requests import Request
from starlette.routing import Route

from tether.active_user_evidence import (
    ActiveUserEvidenceError,
    resolve_active_user_evidence,
)
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.conversations import ConversationService
from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.plans import (
    HealthPlanConflictError,
    HealthPlanDraft,
    HealthPlanEvidence,
    HealthPlanNotFoundError,
    HealthPlanService,
    HealthPlanStatus,
    InvalidHealthPlanError,
)
from tether.health_connect.telemetry import HealthConnectTelemetry
from tether.tool_runtime import ToolSpec


class _HealthConnectToolsRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    conversation_service: ConversationService
    health_connect_telemetry: HealthConnectTelemetry
    health_plan_service: HealthPlanService
    trace_recorder: AgentTraceRecorder


def _runtime(request: Request) -> _HealthConnectToolsRuntime:
    """Read the installed application runtime off the request."""
    return cast("_HealthConnectToolsRuntime", request.app.state.runtime)


class AnalyzeHealthConnectParams(BaseModel):
    """Analyze compact measured Health observations without raw-record joins.

    Use `sleep_episode` with `nap` for the latest nap, or `primary_sleep` for
    last night. Use `sleep_trend` for daily composition and comparable weeks,
    `sleeping_heart_rate` for sleep-aligned personal trends, and
    `metric_status` with `record_type` when a measurement appears missing.
    """

    days: int = Field(default=30, ge=1, le=31)
    episode_kind: Literal["latest", "nap", "primary_sleep"] = "latest"
    focus: Literal[
        "metric_status", "sleep_episode", "sleep_trend", "sleeping_heart_rate"
    ]
    record_type: HealthRecordType | None = None

    @model_validator(mode="after")
    def metric_status_requires_record_type(self) -> Self:
        """Require a metric name only for availability inspection."""
        if self.focus == "metric_status" and self.record_type is None:
            raise HealthConnectMetricStatusRecordTypeError
        return self


class HealthConnectInventoryParams(BaseModel):
    """List populated Health Connect record types and their UTC time bounds."""


class ListHealthPlansParams(BaseModel):
    """List a bounded set of current active and paused exercise intentions."""

    limit: int = Field(default=50, ge=1, le=100)


class SetHealthPlanStatusParams(BaseModel):
    """Pause or resume a Health plan at its observed version."""

    plan_id: UUID7
    status: HealthPlanStatus
    version: PositiveInt


class UpdateHealthPlanParams(HealthPlanDraft):
    """Replace a complete Health plan definition at its observed version."""

    plan_id: UUID7
    version: PositiveInt


class HealthConnectMetricStatusRecordTypeError(ValueError):
    """Metric status requires a supported Health Connect record type."""

    def __init__(self) -> None:
        super().__init__("record_type is required for metric_status")


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


async def _active_health_plan_evidence(request: Request) -> HealthPlanEvidence:
    """Resolve the foreground user Message authorizing a plan mutation."""
    runtime = _runtime(request)
    try:
        source = await resolve_active_user_evidence(
            conversation_service=runtime.conversation_service,
            trace_recorder=runtime.trace_recorder,
            session_id=request.state.session_id,
        )
    except ActiveUserEvidenceError as error:
        message = "Health plan changes require active interactive user Evidence"
        raise InvalidHealthPlanError(message) from error
    return HealthPlanEvidence(
        conversation_id=UUID(str(source.conversation_id)),
        message_id=UUID(str(source.id)),
        occurred_at=source.created_at,
    )


async def _create_health_plan(
    request: Request, params: HealthPlanDraft
) -> CapabilityOutcome:
    """Create a typed weekly exercise intention from fresh user Evidence."""
    plan = await _runtime(request).health_plan_service.create(
        params,
        evidence=await _active_health_plan_evidence(request),
    )
    return CapabilityOutcome(result=plan.model_dump(mode="json"))


async def _update_health_plan(
    request: Request, params: UpdateHealthPlanParams
) -> CapabilityOutcome:
    """Apply one Evidence-backed complete plan revision."""
    plan = await _runtime(request).health_plan_service.update(
        UUID(str(params.plan_id)),
        HealthPlanDraft.model_validate(
            params.model_dump(exclude={"plan_id", "version"})
        ),
        evidence=await _active_health_plan_evidence(request),
        version=params.version,
    )
    return CapabilityOutcome(result=plan.model_dump(mode="json"))


async def _set_health_plan_status(
    request: Request, params: SetHealthPlanStatusParams
) -> CapabilityOutcome:
    """Apply an Evidence-backed pause or resume transition."""
    plan = await _runtime(request).health_plan_service.set_status(
        UUID(str(params.plan_id)),
        evidence=await _active_health_plan_evidence(request),
        status=params.status,
        version=params.version,
    )
    return CapabilityOutcome(result=plan.model_dump(mode="json"))


async def _analyze_health_connect(
    request: Request, params: AnalyzeHealthConnectParams
) -> CapabilityOutcome:
    """Return an episode-aware deterministic Health observation."""
    telemetry = _runtime(request).health_connect_telemetry
    if params.focus == "metric_status":
        insight = await telemetry.insights.fetch_metric_status(
            record_type=cast("HealthRecordType", params.record_type)
        )
    elif params.focus == "sleep_episode":
        insight = await telemetry.insights.fetch_sleep_episode(
            days=params.days, episode_kind=params.episode_kind
        )
    elif params.focus == "sleep_trend":
        insight = await telemetry.insights.fetch_sleep_trend(days=params.days)
    else:
        insight = await telemetry.insights.fetch_sleeping_heart_rate(days=params.days)
    return CapabilityOutcome(result=insight.model_dump(mode="json"))


async def _list_health_plans(request: Request, limit: int) -> CapabilityOutcome:
    """Return current typed Health plans without requiring fresh Evidence."""
    plans = await _runtime(request).health_plan_service.list(limit=limit)
    return CapabilityOutcome(result=[plan.model_dump(mode="json") for plan in plans])


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


_HEALTH_PLAN_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((HealthPlanNotFoundError,), "not_found", 404),
    ErrorRule((HealthPlanConflictError,), "conflict", 409),
    ErrorRule((InvalidHealthPlanError,), "invalid_input", 422),
)


HEALTH_CONNECT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "analyze_health_connect",
        AnalyzeHealthConnectParams,
        _analyze_health_connect,
    ),
    ToolSpec(
        "create_health_plan",
        HealthPlanDraft,
        _create_health_plan,
        _HEALTH_PLAN_ERRORS,
    ),
    ToolSpec(
        "health_connect_inventory",
        HealthConnectInventoryParams,
        bind_params(_health_connect_inventory),
    ),
    ToolSpec(
        "list_health_plans",
        ListHealthPlansParams,
        bind_params(_list_health_plans),
    ),
    ToolSpec(
        "query_health_connect",
        QueryHealthConnectParams,
        _query_health_connect,
    ),
    ToolSpec(
        "set_health_plan_status",
        SetHealthPlanStatusParams,
        _set_health_plan_status,
        _HEALTH_PLAN_ERRORS,
    ),
    ToolSpec(
        "summarize_health_connect",
        SummarizeHealthConnectParams,
        _summarize_health_connect,
    ),
    ToolSpec(
        "update_health_plan",
        UpdateHealthPlanParams,
        _update_health_plan,
        _HEALTH_PLAN_ERRORS,
    ),
)
"""Read-only Health Connect capabilities exposed as internal tools."""


def internal_health_connect_tool_routes() -> list[Route]:
    """Mount Health Connect reads under `/internal/tools/*`."""
    return [spec.route() for spec in HEALTH_CONNECT_TOOL_SPECS]
