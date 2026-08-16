"""Authenticated Health Connect ingestion HTTP routes."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Query
from snekok import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.health_connect_contracts import (
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
from tether.health_connect_ingestion import (
    HealthConnectContractFailure,
    HealthConnectCursorConflict,
    HealthConnectDuplicateRecordTypes,
    HealthConnectIngestion,
    HealthConnectRequestIdentityConflict,
)
from tether.structured_logging import Logger

router = APIRouter()


def _contract_failure_detail(failure: HealthConnectContractFailure) -> str:
    """Present typed record-set failures using the stable wire wording."""
    if isinstance(failure, HealthConnectDuplicateRecordTypes):
        return "record_types must not contain duplicates"
    return "record_types contains unsupported values"


class _HealthConnectRuntime(Protocol):
    """Health Connect dependencies available while the host serves requests."""

    health_connect_ingestion: HealthConnectIngestion
    logger: Logger


def _runtime(request: Request) -> _HealthConnectRuntime:
    """Read Health Connect dependencies from the canonical host runtime."""
    return cast("_HealthConnectRuntime", request.app.state.runtime)


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
