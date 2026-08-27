"""Authenticated Health Connect ingestion HTTP routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, model_validator
from snekok import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.health_connect.contracts import (
    CompleteHealthConnectBaselineRequest,
    HealthConnectBaselineCompletionRead,
    HealthConnectBatchRead,
    HealthConnectBatchRequest,
    HealthConnectContractError,
    HealthConnectSyncStateQuery,
    HealthConnectSyncStateRead,
    StartHealthConnectBaselineRequest,
    canonical_record_types,
    parse_record_types,
    validate_versioned_record_types,
)
from tether.health_connect.ingestion import (
    HealthConnectContractFailure,
    HealthConnectCursorConflict,
    HealthConnectDuplicateRecordTypes,
    HealthConnectIngestion,
    HealthConnectRequestIdentityConflict,
)
from tether.health_connect.insight_model import HealthConnectSleepEpisodeInsightRead
from tether.health_connect.moments import HealthMomentService
from tether.health_connect.telemetry import HealthConnectTelemetry
from tether.health_connect.telemetry_model import HealthConnectSummaryRead
from tether.structured_logging import Logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from snekql.sqlite import Fetched

    from tether.dreaming_store import HealthDreamRun

router = APIRouter()


def _contract_failure_detail(failure: HealthConnectContractFailure) -> str:
    """Present typed record-set failures using the stable wire wording."""
    if isinstance(failure, HealthConnectDuplicateRecordTypes):
        return "record_types must not contain duplicates"
    return "record_types contains unsupported values"


class HealthDreamNowRequest(BaseModel):
    """Optional episode period bounding a manual consolidation run."""

    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def _period_is_ordered(self) -> HealthDreamNowRequest:
        if self.start is not None and self.end is not None and self.start >= self.end:
            message = "start must precede end"
            raise ValueError(message)
        return self


class HealthOverviewMomentRead(BaseModel):
    """One proactive briefing identity linked to its exact chat turn."""

    evidence_uri: str
    id: UUID
    kind: Literal["exercise", "primary_sleep"]
    observed_at: AwareDatetime
    status: Literal["pending", "running", "succeeded", "failed"]
    turn_id: UUID | None


class HealthOverviewRead(BaseModel):
    """Measured Health observations and linked proactive briefings."""

    after: AwareDatetime
    before: AwareDatetime
    days: int
    latest_observation_at: AwareDatetime | None
    moments: list[HealthOverviewMomentRead]
    primary_sleep: HealthConnectSleepEpisodeInsightRead
    summary: HealthConnectSummaryRead


class _HealthDistillationPort(Protocol):
    """Manual-trigger interface of the health consolidation service."""

    telemetry_database: object

    def queue_run(self) -> Awaitable[HealthDreamRun[Fetched] | None]: ...

    def drain_backlog(self) -> Awaitable[list[HealthDreamRun[Fetched]]]: ...

    def queue_explicit_run(
        self, *, start: datetime, end: datetime
    ) -> Awaitable[HealthDreamRun[Fetched] | None]: ...


class _HealthConnectRuntime(Protocol):
    """Health Connect dependencies available while the host serves requests."""

    dreaming_enabled: bool
    health_connect_ingestion: HealthConnectIngestion
    health_connect_telemetry: HealthConnectTelemetry
    health_distillation_service: _HealthDistillationPort | None
    health_moment_service: HealthMomentService
    logger: Logger


def _runtime(request: Request) -> _HealthConnectRuntime:
    """Read Health Connect dependencies from the canonical host runtime."""
    return cast("_HealthConnectRuntime", request.app.state.runtime)


@router.get("/api/health/overview", response_model=HealthOverviewRead)
async def read_health_overview(
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    before: AwareDatetime | None = None,
) -> Response:
    """Read one bounded Health page projection without agent interpretation."""
    runtime = _runtime(request)
    period_end = datetime.now(UTC) if before is None else before
    period_start = period_end - timedelta(days=days)
    summary = await runtime.health_connect_telemetry.summary.fetch_summary(
        after=period_start,
        before=period_end,
        bucket="day",
    )
    primary_sleep = await runtime.health_connect_telemetry.insights.fetch_sleep_episode(
        days=days,
        episode_kind="primary_sleep",
    )
    if primary_sleep.selected_episode is not None and not (
        period_start <= primary_sleep.selected_episode.local_end <= period_end
    ):
        primary_sleep = HealthConnectSleepEpisodeInsightRead(
            requested_days=days,
            selected_episode=None,
            status="no_matching_episode",
        )
    inventory = await runtime.health_connect_telemetry.inventory.fetch_inventory()
    observation_bounds = [
        entry.latest_end
        for entry in inventory
        if entry.latest_end is not None and entry.latest_end <= period_end
    ]
    moments = [
        HealthOverviewMomentRead(
            evidence_uri=moment.evidence_uri,
            id=moment.id,
            kind=moment.kind,
            observed_at=moment.observed_at,
            status=moment.status,
            turn_id=moment.turn_id,
        )
        for moment in await runtime.health_moment_service.list_recent(limit=50)
        if period_start <= moment.observed_at <= period_end
    ]
    overview = HealthOverviewRead(
        after=period_start,
        before=period_end,
        days=days,
        latest_observation_at=max(observation_bounds, default=None),
        moments=moments,
        primary_sleep=primary_sleep,
        summary=summary,
    )
    return JSONResponse(overview.model_dump(mode="json"))


@router.get(
    "/api/telemetry/health-connect/sync-state",
    response_model=HealthConnectSyncStateRead,
)
async def read_health_connect_sync_state(
    request: Request, query: Annotated[HealthConnectSyncStateQuery, Query()]
) -> Response:
    try:
        record_types = parse_record_types(query.record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = _runtime(request).health_connect_ingestion
    return JSONResponse(
        (
            await service.fetch_sync_state(query.installation_id, record_types)
        ).model_dump(mode="json")
    )


@router.post(
    "/api/telemetry/health-connect/sync-state/baselines",
    response_model=HealthConnectSyncStateRead,
    status_code=201,
)
async def start_health_connect_baseline(
    request: Request, body: StartHealthConnectBaselineRequest
) -> Response:
    try:
        record_types = canonical_record_types(list(body.record_types))
        validate_versioned_record_types(body.contract_version, record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = _runtime(request).health_connect_ingestion
    state = await service.start_baseline(
        installation_id=body.installation_id,
        record_types=record_types,
        starting_token=body.starting_token,
        request_id=body.request_id,
    )
    _runtime(request).logger.info(
        "Health Connect baseline started",
        baseline_generation=state.baseline_generation,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(state.model_dump(mode="json"), status_code=201)


@router.post(
    "/api/telemetry/health-connect/sync-state/baselines/complete",
    response_model=HealthConnectBaselineCompletionRead,
)
async def complete_health_connect_baseline(
    request: Request, body: CompleteHealthConnectBaselineRequest
) -> Response:
    """Reconcile bounded baseline absence and unlock live change pages."""
    service = _runtime(request).health_connect_ingestion
    outcome = await service.complete_baseline(body)
    if isinstance(outcome, Err):
        if isinstance(outcome.error, HealthConnectCursorConflict):
            _runtime(request).logger.warning(
                "Health Connect baseline completion conflicted",
                error_category="cursor_conflict",
                installation_id=body.installation_id,
                request_id=body.request_id,
            )
            return JSONResponse({"detail": "baseline state is stale"}, status_code=409)
        return JSONResponse(
            {"detail": _contract_failure_detail(outcome.error)}, status_code=422
        )
    report = outcome.value
    _runtime(request).logger.info(
        "Health Connect baseline completed",
        deleted=report.deleted,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(report.model_dump(mode="json"))


@router.post(
    "/api/telemetry/health-connect/batches", response_model=HealthConnectBatchRead
)
async def ingest_health_connect_batch(
    request: Request, body: HealthConnectBatchRequest
) -> Response:
    service = _runtime(request).health_connect_ingestion
    outcome = await service.ingest_batch(body)
    if isinstance(outcome, Err):
        if isinstance(outcome.error, HealthConnectCursorConflict):
            _runtime(request).logger.warning(
                "Health Connect page conflicted",
                error_category="cursor_conflict",
                installation_id=body.installation_id,
                request_id=body.request_id,
            )
            return JSONResponse({"detail": "expected token is stale"}, status_code=409)
        if isinstance(outcome.error, HealthConnectRequestIdentityConflict):
            return JSONResponse(
                {"detail": "request_id was reused for another page"}, status_code=409
            )
        return JSONResponse(
            {"detail": _contract_failure_detail(outcome.error)}, status_code=409
        )
    report = outcome.value
    _runtime(request).logger.info(
        "Health Connect page accepted",
        accepted=report.accepted,
        deleted=report.deleted,
        installation_id=body.installation_id,
        replayed=report.replayed,
        request_id=body.request_id,
        skipped=report.skipped,
    )
    return JSONResponse(report.model_dump(mode="json"))


@router.post("/api/telemetry/health-connect/dream-now")
async def health_dream_now(
    request: Request,
    body: HealthDreamNowRequest | None = None,
) -> Response:
    """Queue manual consolidation runs over Health Connect summaries.

    Without a period, every summary not yet captured by a prior run is
    windowed into successive capped runs (bounded prompts) and all of them
    are queued. With `{start, end}`, only episodes ending inside the period
    are reconsidered, as one run.
    """
    runtime = _runtime(request)
    service = runtime.health_distillation_service
    if not runtime.dreaming_enabled or service is None:
        return JSONResponse({"detail": "dreaming not enabled"}, status_code=404)
    if body is not None and body.start is not None and body.end is not None:
        run = await service.queue_explicit_run(start=body.start, end=body.end)
        runs = [] if run is None else [run]
    else:
        runs = await service.drain_backlog()
    if not runs:
        return Response(status_code=204)
    return JSONResponse([_health_dream_run_payload(run) for run in runs])


def _health_dream_run_payload(run: HealthDreamRun[Fetched]) -> dict[str, object]:
    """Render one queued health dream run for the wire."""
    return {
        "id": str(run.id),
        "status": run.status,
        "exercise_since_version_id": run.exercise_since_version_id,
        "exercise_through_version_id": run.exercise_through_version_id,
        "sleep_since_version_id": run.sleep_since_version_id,
        "sleep_through_version_id": run.sleep_through_version_id,
        "attempts": run.attempts,
    }
