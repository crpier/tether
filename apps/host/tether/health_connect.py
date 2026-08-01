"""Typed, append-only Health Connect telemetry store and sync HTTP contract."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from snekql.sqlite import (
    PENDING_GENERATION,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Real,
    Text,
    Transaction,
    insert,
    scaffold,
    select,
    update,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger
from tether.openapi import EndpointRoute, endpoint

HealthRecordType = Literal["exercise", "heart_rate", "sleep", "steps"]
_ALLOWED_RECORD_TYPES = frozenset({"exercise", "heart_rate", "sleep", "steps"})
RecordStatus = Literal["baseline", "changes", "initial"]


class HealthConnectSyncState[S = Pending](Model[S, "HealthConnectSyncState[Fetched]"]):
    """Durable cursor and baseline generation for one installation/type set."""

    __tablename__ = "hc_sync_state"
    state_key: HealthConnectSyncState.Col[str] = Text(primary_key=True)
    baseline_generation: HealthConnectSyncState.Col[int] = Integer(nullable=False)
    baseline_request_id: HealthConnectSyncState.Col[str | None] = Text(nullable=True)
    completion_deleted_json: HealthConnectSyncState.Col[str | None] = Text(
        nullable=True
    )
    completion_request_id: HealthConnectSyncState.Col[str | None] = Text(nullable=True)
    current_token: HealthConnectSyncState.Col[str | None] = Text(nullable=True)
    installation_id: HealthConnectSyncState.Col[str] = Text(nullable=False, index=True)
    record_type_set: HealthConnectSyncState.Col[str] = Text(nullable=False)
    status: HealthConnectSyncState.Col[str] = Text(nullable=False)


class HcOrigin[S = Pending](Model[S, "HcOrigin[Fetched]"]):
    """Writing application and nullable device provenance."""

    origin_id: HcOrigin.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    origin_key: HcOrigin.Col[str] = Text(nullable=False, unique=True)
    data_origin_package: HcOrigin.Col[str] = Text(nullable=False)
    device_manufacturer: HcOrigin.Col[str | None] = Text(nullable=True)
    device_model: HcOrigin.Col[str | None] = Text(nullable=True)
    device_type: HcOrigin.Col[int | None] = Integer(nullable=True)


class HcPageRequest[S = Pending](Model[S, "HcPageRequest[Fetched]"]):
    """Committed request identity making response-loss retries idempotent."""

    request_id: HcPageRequest.Col[str] = Text(primary_key=True)
    state_key: HcPageRequest.Col[str] = Text(nullable=False, index=True)
    payload_hash: HcPageRequest.Col[str] = Text(nullable=False)
    accepted_json: HcPageRequest.Col[str] = Text(nullable=False)
    deleted_json: HcPageRequest.Col[str] = Text(nullable=False)
    skipped_json: HcPageRequest.Col[str] = Text(nullable=False)


class HcHeartRateRecord[S = Pending](Model[S, "HcHeartRateRecord[Fetched]"]):
    """One accepted heart-rate record version or tombstone."""

    version_id: HcHeartRateRecord.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: HcHeartRateRecord.Col[str] = Text(nullable=False, index=True)
    origin_id: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)
    modified_at: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)
    received_at: HcHeartRateRecord.Col[int] = Integer(nullable=False)
    request_id: HcHeartRateRecord.Col[str] = Text(nullable=False, index=True)
    is_deleted: HcHeartRateRecord.Col[bool] = Integer(nullable=False)
    payload_hash: HcHeartRateRecord.Col[str] = Text(nullable=False)
    client_record_id: HcHeartRateRecord.Col[str | None] = Text(nullable=True)
    client_record_version: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)
    recording_method: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)
    start_time: HcHeartRateRecord.Col[int | None] = Integer(nullable=True, index=True)
    end_time: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcHeartRateRecord.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcHeartRateRecord.Col[int | None] = Integer(nullable=True)


class HcHeartRateSample[S = Pending](Model[S, "HcHeartRateSample[Fetched]"]):
    """An ordered sample belonging to exactly one heart-rate version."""

    sample_id: HcHeartRateSample.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: HcHeartRateSample.Col[int] = Integer(nullable=False, index=True)
    sample_index: HcHeartRateSample.Col[int] = Integer(nullable=False)
    time: HcHeartRateSample.Col[int] = Integer(nullable=False, index=True)
    beats_per_minute: HcHeartRateSample.Col[int] = Integer(nullable=False)


class HcSleepSession[S = Pending](Model[S, "HcSleepSession[Fetched]"]):
    """One accepted sleep-session version or tombstone."""

    version_id: HcSleepSession.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: HcSleepSession.Col[str] = Text(nullable=False, index=True)
    origin_id: HcSleepSession.Col[int | None] = Integer(nullable=True)
    modified_at: HcSleepSession.Col[int | None] = Integer(nullable=True)
    received_at: HcSleepSession.Col[int] = Integer(nullable=False)
    request_id: HcSleepSession.Col[str] = Text(nullable=False, index=True)
    is_deleted: HcSleepSession.Col[bool] = Integer(nullable=False)
    payload_hash: HcSleepSession.Col[str] = Text(nullable=False)
    client_record_id: HcSleepSession.Col[str | None] = Text(nullable=True)
    client_record_version: HcSleepSession.Col[int | None] = Integer(nullable=True)
    recording_method: HcSleepSession.Col[int | None] = Integer(nullable=True)
    start_time: HcSleepSession.Col[int | None] = Integer(nullable=True, index=True)
    end_time: HcSleepSession.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcSleepSession.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: HcSleepSession.Col[int | None] = Integer(nullable=True)
    title: HcSleepSession.Col[str | None] = Text(nullable=True)
    notes: HcSleepSession.Col[str | None] = Text(nullable=True)


class HcSleepStage[S = Pending](Model[S, "HcSleepStage[Fetched]"]):
    """An ordered original-enum stage belonging to one sleep version."""

    stage_id: HcSleepStage.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: HcSleepStage.Col[int] = Integer(nullable=False, index=True)
    stage_index: HcSleepStage.Col[int] = Integer(nullable=False)
    start_time: HcSleepStage.Col[int] = Integer(nullable=False, index=True)
    end_time: HcSleepStage.Col[int] = Integer(nullable=False)
    stage: HcSleepStage.Col[int] = Integer(nullable=False)


class HcStepInterval[S = Pending](Model[S, "HcStepInterval[Fetched]"]):
    """One accepted step interval version or tombstone."""

    version_id: HcStepInterval.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: HcStepInterval.Col[str] = Text(nullable=False, index=True)
    origin_id: HcStepInterval.Col[int | None] = Integer(nullable=True)
    modified_at: HcStepInterval.Col[int | None] = Integer(nullable=True)
    received_at: HcStepInterval.Col[int] = Integer(nullable=False)
    request_id: HcStepInterval.Col[str] = Text(nullable=False, index=True)
    is_deleted: HcStepInterval.Col[bool] = Integer(nullable=False)
    payload_hash: HcStepInterval.Col[str] = Text(nullable=False)
    client_record_id: HcStepInterval.Col[str | None] = Text(nullable=True)
    client_record_version: HcStepInterval.Col[int | None] = Integer(nullable=True)
    recording_method: HcStepInterval.Col[int | None] = Integer(nullable=True)
    start_time: HcStepInterval.Col[int | None] = Integer(nullable=True, index=True)
    end_time: HcStepInterval.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcStepInterval.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: HcStepInterval.Col[int | None] = Integer(nullable=True)
    count: HcStepInterval.Col[int | None] = Integer(nullable=True)


class HcExerciseSession[S = Pending](Model[S, "HcExerciseSession[Fetched]"]):
    """One accepted exercise-session version or tombstone."""

    version_id: HcExerciseSession.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: HcExerciseSession.Col[str] = Text(nullable=False, index=True)
    origin_id: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    modified_at: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    received_at: HcExerciseSession.Col[int] = Integer(nullable=False)
    request_id: HcExerciseSession.Col[str] = Text(nullable=False, index=True)
    is_deleted: HcExerciseSession.Col[bool] = Integer(nullable=False)
    payload_hash: HcExerciseSession.Col[str] = Text(nullable=False)
    client_record_id: HcExerciseSession.Col[str | None] = Text(nullable=True)
    client_record_version: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    recording_method: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    start_time: HcExerciseSession.Col[int | None] = Integer(nullable=True, index=True)
    end_time: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcExerciseSession.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    exercise_type: HcExerciseSession.Col[int | None] = Integer(nullable=True)
    title: HcExerciseSession.Col[str | None] = Text(nullable=True)
    notes: HcExerciseSession.Col[str | None] = Text(nullable=True)
    planned_exercise_session_id: HcExerciseSession.Col[str | None] = Text(nullable=True)


class HcExerciseSegment[S = Pending](Model[S, "HcExerciseSegment[Fetched]"]):
    """An ordered segment belonging to one exercise version."""

    segment_id: HcExerciseSegment.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: HcExerciseSegment.Col[int] = Integer(nullable=False, index=True)
    segment_index: HcExerciseSegment.Col[int] = Integer(nullable=False)
    start_time: HcExerciseSegment.Col[int] = Integer(nullable=False)
    end_time: HcExerciseSegment.Col[int] = Integer(nullable=False)
    segment_type: HcExerciseSegment.Col[int] = Integer(nullable=False)
    repetitions_count: HcExerciseSegment.Col[int] = Integer(nullable=False)


class HcExerciseLap[S = Pending](Model[S, "HcExerciseLap[Fetched]"]):
    """An ordered lap with canonical meter length."""

    lap_id: HcExerciseLap.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: HcExerciseLap.Col[int] = Integer(nullable=False, index=True)
    lap_index: HcExerciseLap.Col[int] = Integer(nullable=False)
    start_time: HcExerciseLap.Col[int] = Integer(nullable=False)
    end_time: HcExerciseLap.Col[int] = Integer(nullable=False)
    length_meters: HcExerciseLap.Col[float | None] = Real(nullable=True)


class HcExerciseRoutePoint[S = Pending](Model[S, "HcExerciseRoutePoint[Fetched]"]):
    """An ordered route point with Health Connect's canonical units."""

    route_point_id: HcExerciseRoutePoint.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: HcExerciseRoutePoint.Col[int] = Integer(nullable=False, index=True)
    point_index: HcExerciseRoutePoint.Col[int] = Integer(nullable=False)
    time: HcExerciseRoutePoint.Col[int] = Integer(nullable=False, index=True)
    latitude: HcExerciseRoutePoint.Col[float] = Real(nullable=False)
    longitude: HcExerciseRoutePoint.Col[float] = Real(nullable=False)
    horizontal_accuracy_meters: HcExerciseRoutePoint.Col[float | None] = Real(
        nullable=True
    )
    vertical_accuracy_meters: HcExerciseRoutePoint.Col[float | None] = Real(
        nullable=True
    )
    altitude_meters: HcExerciseRoutePoint.Col[float | None] = Real(nullable=True)


class HealthConnectWireModel(BaseModel):
    """Strict base for the versioned Android/host JSON boundary."""

    model_config = ConfigDict(extra="forbid")


class Device(HealthConnectWireModel):
    """Nullable Health Connect writing-device metadata."""

    manufacturer: str | None = None
    model: str | None = None
    type: int | None = None


class RecordMetadata(HealthConnectWireModel):
    """Common metadata exposed by the pinned Health Connect wire contract."""

    id: str = Field(min_length=1)
    data_origin_package: str = Field(min_length=1)
    last_modified_time: int | None
    client_record_id: str | None
    client_record_version: int | None
    device: Device | None
    recording_method: int | None


class HeartRateSample(HealthConnectWireModel):
    time: int
    beats_per_minute: int = Field(gt=0)


class HeartRateRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    samples: list[HeartRateSample] = Field(max_length=10_000)


class SleepStage(HealthConnectWireModel):
    start_time: int
    end_time: int
    stage: int


class SleepRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    title: str | None
    notes: str | None
    stages: list[SleepStage] = Field(max_length=1_000)


class StepsRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    count: int = Field(ge=0)


class ExerciseSegment(HealthConnectWireModel):
    start_time: int
    end_time: int
    segment_type: int
    repetitions_count: int = Field(ge=0)


class ExerciseLap(HealthConnectWireModel):
    start_time: int
    end_time: int
    length_meters: float | None


class ExerciseRoutePoint(HealthConnectWireModel):
    time: int
    latitude: float
    longitude: float
    horizontal_accuracy_meters: float | None
    vertical_accuracy_meters: float | None
    altitude_meters: float | None


class ExerciseRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    exercise_type: int
    title: str | None
    notes: str | None
    planned_exercise_session_id: str | None
    segments: list[ExerciseSegment] = Field(max_length=10_000)
    laps: list[ExerciseLap] = Field(max_length=10_000)
    route: list[ExerciseRoutePoint] = Field(max_length=100_000)


class HealthConnectRecords(HealthConnectWireModel):
    exercise: list[ExerciseRecord] = Field(max_length=1_000)
    heart_rate: list[HeartRateRecord] = Field(max_length=1_000)
    sleep: list[SleepRecord] = Field(max_length=1_000)
    steps: list[StepsRecord] = Field(max_length=1_000)


class HealthConnectDeletion(HealthConnectWireModel):
    record_type: HealthRecordType
    record_id: str = Field(min_length=1)


class HealthConnectBatchRequest(HealthConnectWireModel):
    contract_version: Literal[1]
    mode: Literal["baseline", "changes"]
    installation_id: str = Field(min_length=1)
    record_types: list[HealthRecordType]
    request_id: str = Field(min_length=1)
    expected_token: str
    next_token: str
    records: HealthConnectRecords
    deletions: list[HealthConnectDeletion] = Field(max_length=10_000)


class AuthoritativeScanRange(HealthConnectWireModel):
    """Exact time range and IDs returned authoritatively by Health Connect."""

    start_time: int
    end_time: int
    seen_record_ids: list[str] = Field(max_length=100_000)


class HealthConnectBaselineRanges(HealthConnectWireModel):
    """Authoritative baseline declarations for every contract record type."""

    exercise: AuthoritativeScanRange
    heart_rate: AuthoritativeScanRange
    sleep: AuthoritativeScanRange
    steps: AuthoritativeScanRange


class CompleteHealthConnectBaselineRequest(HealthConnectWireModel):
    """Bounded authoritative scan used to reconcile expired-token gaps."""

    contract_version: Literal[1]
    installation_id: str
    record_types: list[HealthRecordType]
    request_id: str
    expected_token: str
    baseline_generation: int = Field(gt=0)
    ranges: HealthConnectBaselineRanges


class HealthConnectBaselineCompletionRead(HealthConnectWireModel):
    """Safe operational counts from baseline reconciliation."""

    deleted: dict[HealthRecordType, int]
    status: Literal["completed"]


class HealthConnectSyncStateQuery(HealthConnectWireModel):
    installation_id: str
    record_types: str


class StartHealthConnectBaselineRequest(HealthConnectWireModel):
    contract_version: Literal[1]
    installation_id: str
    record_types: list[HealthRecordType]
    request_id: str
    starting_token: str


class HealthConnectSyncStateRead(HealthConnectWireModel):
    baseline_generation: int
    current_token: str | None
    installation_id: str
    record_types: list[HealthRecordType]
    status: RecordStatus


class HealthConnectBatchRead(HealthConnectWireModel):
    accepted: dict[HealthRecordType, int]
    deleted: dict[HealthRecordType, int]
    replayed: bool
    skipped: dict[HealthRecordType, int]
    status: Literal["accepted"]


class HealthConnectContractError(Exception):
    """Malformed stream identity or request reuse."""


class UnsupportedRecordTypesError(HealthConnectContractError):
    """The stream contains an unknown record type."""

    def __init__(self) -> None:
        super().__init__("record_types contains unsupported values")


class DuplicateRecordTypesError(HealthConnectContractError):
    """The stream repeats a record type."""

    def __init__(self) -> None:
        super().__init__("record_types must not contain duplicates")


class RequestIdentityReuseError(HealthConnectContractError):
    """A committed request ID was presented with different content."""

    def __init__(self) -> None:
        super().__init__("request_id was reused for another page")


class HealthConnectCursorConflictError(Exception):
    """The page expected a cursor that is no longer current."""


_MODELS = [
    HealthConnectSyncState,
    HcOrigin,
    HcPageRequest,
    HcHeartRateRecord,
    HcHeartRateSample,
    HcSleepSession,
    HcSleepStage,
    HcStepInterval,
    HcExerciseSession,
    HcExerciseSegment,
    HcExerciseLap,
    HcExerciseRoutePoint,
]
_PARENT_MODELS = {
    "exercise": HcExerciseSession,
    "heart_rate": HcHeartRateRecord,
    "sleep": HcSleepSession,
    "steps": HcStepInterval,
}


def _empty_counts() -> dict[HealthRecordType, int]:
    return {"exercise": 0, "heart_rate": 0, "sleep": 0, "steps": 0}


def _state_key(installation_id: str, record_types: tuple[HealthRecordType, ...]) -> str:
    return f"{installation_id}\x1f{','.join(record_types)}"


def _parse_record_types(raw: str) -> tuple[HealthRecordType, ...]:
    values = set(raw.split(","))
    if "" in values or not values or not values <= _ALLOWED_RECORD_TYPES:
        raise UnsupportedRecordTypesError
    return cast("tuple[HealthRecordType, ...]", tuple(sorted(values)))


def _canonical_record_types(raw: list[str]) -> tuple[HealthRecordType, ...]:
    if len(set(raw)) != len(raw):
        raise DuplicateRecordTypesError
    return _parse_record_types(",".join(raw))


def _state_read(stored: HealthConnectSyncState[Fetched]) -> HealthConnectSyncStateRead:
    return HealthConnectSyncStateRead(
        baseline_generation=stored.baseline_generation,
        current_token=stored.current_token,
        installation_id=stored.installation_id,
        record_types=list(_parse_record_types(stored.record_type_set)),
        status=cast("RecordStatus", stored.status),
    )


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _missing_current_ids(
    rows: Sequence[
        HcExerciseSession[Fetched]
        | HcHeartRateRecord[Fetched]
        | HcSleepSession[Fetched]
        | HcStepInterval[Fetched]
    ],
    scan: AuthoritativeScanRange,
) -> list[str]:
    """Find latest live IDs wholly inside an authoritative range but unseen."""
    seen_versions: set[str] = set()
    missing: list[str] = []
    authoritative_ids = set(scan.seen_record_ids)
    for row in rows:
        if row.record_uid in seen_versions:
            continue
        seen_versions.add(row.record_uid)
        if (
            not row.is_deleted
            and row.start_time is not None
            and row.end_time is not None
            and row.start_time >= scan.start_time
            and row.end_time <= scan.end_time
            and row.record_uid not in authoritative_ids
        ):
            missing.append(row.record_uid)
    return missing


async def _origin_id(transaction: Transaction, metadata: RecordMetadata) -> int:
    device = metadata.device
    origin_fields = {
        "data_origin_package": metadata.data_origin_package,
        "device_manufacturer": None if device is None else device.manufacturer,
        "device_model": None if device is None else device.model,
        "device_type": None if device is None else device.type,
    }
    origin_key = _hash_json(origin_fields)
    existing = await transaction.fetch_one_or_none(
        select(HcOrigin).where(HcOrigin.origin_key.eq(origin_key))
    )
    if existing is not None:
        return existing.origin_id
    created = await transaction.execute(
        insert(
            HcOrigin(
                data_origin_package=metadata.data_origin_package,
                device_manufacturer=None if device is None else device.manufacturer,
                device_model=None if device is None else device.model,
                device_type=None if device is None else device.type,
                origin_key=origin_key,
            )
        ).returning()
    )
    return created.origin_id


@dataclass(frozen=True, slots=True)
class HealthConnectService:
    """Atomic Health Connect cursor and append operations."""

    database: Database

    async def fetch_sync_state(
        self, installation_id: str, record_types: tuple[HealthRecordType, ...]
    ) -> HealthConnectSyncStateRead:
        async with self.database.transaction() as transaction:
            stored = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(
                        _state_key(installation_id, record_types)
                    )
                )
            )
        if stored is None:
            return HealthConnectSyncStateRead(
                baseline_generation=0,
                current_token=None,
                installation_id=installation_id,
                record_types=list(record_types),
                status="initial",
            )
        return _state_read(stored)

    async def start_baseline(
        self,
        *,
        installation_id: str,
        record_types: tuple[HealthRecordType, ...],
        starting_token: str,
        request_id: str,
    ) -> HealthConnectSyncStateRead:
        key = _state_key(installation_id, record_types)
        async with self.database.transaction() as transaction:
            stored = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if stored is not None and stored.baseline_request_id == request_id:
                return _state_read(stored)
            if stored is None:
                _ = await transaction.execute(
                    insert(
                        HealthConnectSyncState(
                            baseline_generation=1,
                            baseline_request_id=request_id,
                            completion_deleted_json=None,
                            completion_request_id=None,
                            current_token=starting_token,
                            installation_id=installation_id,
                            record_type_set=",".join(record_types),
                            state_key=key,
                            status="baseline",
                        )
                    )
                )
            else:
                _ = await transaction.execute(
                    update(HealthConnectSyncState)
                    .set(
                        HealthConnectSyncState.baseline_generation.to(
                            stored.baseline_generation + 1
                        ),
                        HealthConnectSyncState.baseline_request_id.to(request_id),
                        HealthConnectSyncState.completion_deleted_json.to(None),
                        HealthConnectSyncState.completion_request_id.to(None),
                        HealthConnectSyncState.current_token.to(starting_token),
                        HealthConnectSyncState.status.to("baseline"),
                    )
                    .where(HealthConnectSyncState.state_key.eq(key))
                )
            persisted = await transaction.fetch_one(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
        return _state_read(persisted)

    async def complete_baseline(
        self, body: CompleteHealthConnectBaselineRequest
    ) -> HealthConnectBaselineCompletionRead:
        """Reconcile only bounded authoritative ranges and enter changes mode."""
        record_types = _canonical_record_types(list(body.record_types))
        key = _state_key(body.installation_id, record_types)
        async with self.database.transaction() as transaction:
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is not None and state.completion_request_id == body.request_id:
                return HealthConnectBaselineCompletionRead(
                    deleted=json.loads(state.completion_deleted_json or "{}"),
                    status="completed",
                )
            if (
                state is None
                or state.current_token != body.expected_token
                or state.baseline_generation != body.baseline_generation
                or state.status != "baseline"
            ):
                raise HealthConnectCursorConflictError
            heart_rows = await transaction.fetch_all(
                select(HcHeartRateRecord)
                .all()
                .order_by(HcHeartRateRecord.version_id.desc())
            )
            sleep_rows = await transaction.fetch_all(
                select(HcSleepSession).all().order_by(HcSleepSession.version_id.desc())
            )
            step_rows = await transaction.fetch_all(
                select(HcStepInterval).all().order_by(HcStepInterval.version_id.desc())
            )
            exercise_rows = await transaction.fetch_all(
                select(HcExerciseSession)
                .all()
                .order_by(HcExerciseSession.version_id.desc())
            )
            deletions = [
                *(
                    HealthConnectDeletion(record_type="heart_rate", record_id=uid)
                    for uid in _missing_current_ids(heart_rows, body.ranges.heart_rate)
                ),
                *(
                    HealthConnectDeletion(record_type="sleep", record_id=uid)
                    for uid in _missing_current_ids(sleep_rows, body.ranges.sleep)
                ),
                *(
                    HealthConnectDeletion(record_type="steps", record_id=uid)
                    for uid in _missing_current_ids(step_rows, body.ranges.steps)
                ),
                *(
                    HealthConnectDeletion(record_type="exercise", record_id=uid)
                    for uid in _missing_current_ids(exercise_rows, body.ranges.exercise)
                ),
            ]
            deleted, skipped = _empty_counts(), _empty_counts()
            reconciliation_batch = HealthConnectBatchRequest(
                contract_version=1,
                deletions=deletions,
                expected_token=body.expected_token,
                installation_id=body.installation_id,
                mode="baseline",
                next_token=body.expected_token,
                records=HealthConnectRecords(
                    exercise=[], heart_rate=[], sleep=[], steps=[]
                ),
                record_types=list(record_types),
                request_id=body.request_id,
            )
            await self._append_deletions(
                transaction,
                reconciliation_batch,
                time.time_ns() // 1_000_000,
                deleted,
                skipped,
            )
            _ = await transaction.execute(
                update(HealthConnectSyncState)
                .set(
                    HealthConnectSyncState.completion_deleted_json.to(
                        json.dumps(deleted, sort_keys=True)
                    ),
                    HealthConnectSyncState.completion_request_id.to(body.request_id),
                    HealthConnectSyncState.status.to("changes"),
                )
                .where(HealthConnectSyncState.state_key.eq(key))
            )
        return HealthConnectBaselineCompletionRead(deleted=deleted, status="completed")

    async def ingest_batch(
        self, batch: HealthConnectBatchRequest
    ) -> HealthConnectBatchRead:
        record_types = _canonical_record_types(list(batch.record_types))
        key = _state_key(batch.installation_id, record_types)
        payload_hash = _hash_json(batch.model_dump(mode="json"))
        async with self.database.transaction() as transaction:
            replay = await transaction.fetch_one_or_none(
                select(HcPageRequest).where(
                    HcPageRequest.request_id.eq(batch.request_id)
                )
            )
            if replay is not None:
                if replay.state_key != key or replay.payload_hash != payload_hash:
                    raise RequestIdentityReuseError
                return HealthConnectBatchRead(
                    accepted=json.loads(replay.accepted_json),
                    deleted=json.loads(replay.deleted_json),
                    replayed=True,
                    skipped=json.loads(replay.skipped_json),
                    status="accepted",
                )
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is None or state.current_token != batch.expected_token:
                raise HealthConnectCursorConflictError
            if batch.mode == "baseline" and (
                state.status != "baseline" or batch.next_token != batch.expected_token
            ):
                raise HealthConnectCursorConflictError
            if batch.mode == "changes" and state.status != "changes":
                raise HealthConnectCursorConflictError
            accepted, skipped, deleted = (
                _empty_counts(),
                _empty_counts(),
                _empty_counts(),
            )
            received_at = time.time_ns() // 1_000_000
            await self._append_records(
                transaction, batch, received_at, accepted, skipped
            )
            await self._append_deletions(
                transaction, batch, received_at, deleted, skipped
            )
            _ = await transaction.execute(
                update(HealthConnectSyncState)
                .set(
                    HealthConnectSyncState.current_token.to(batch.next_token),
                    HealthConnectSyncState.status.to(
                        "baseline" if batch.mode == "baseline" else "changes"
                    ),
                )
                .where(HealthConnectSyncState.state_key.eq(key))
            )
            _ = await transaction.execute(
                insert(
                    HcPageRequest(
                        request_id=batch.request_id,
                        state_key=key,
                        payload_hash=payload_hash,
                        accepted_json=json.dumps(accepted, sort_keys=True),
                        deleted_json=json.dumps(deleted, sort_keys=True),
                        skipped_json=json.dumps(skipped, sort_keys=True),
                    )
                )
            )
        return HealthConnectBatchRead(
            accepted=accepted,
            deleted=deleted,
            replayed=False,
            skipped=skipped,
            status="accepted",
        )

    async def _append_deletions(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        deleted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        """Append tombstones, resolving origin only from accepted history."""
        for deletion in batch.deletions:
            if deletion.record_type == "heart_rate":
                latest = await transaction.fetch_one_or_none(
                    select(HcHeartRateRecord)
                    .where(HcHeartRateRecord.record_uid.eq(deletion.record_id))
                    .order_by(HcHeartRateRecord.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.is_deleted:
                    skipped["heart_rate"] += 1
                    continue
                _ = await transaction.execute(
                    insert(
                        HcHeartRateRecord(
                            client_record_id=None,
                            client_record_version=None,
                            end_time=None,
                            end_zone_offset_seconds=None,
                            is_deleted=True,
                            modified_at=None,
                            origin_id=None if latest is None else latest.origin_id,
                            payload_hash=_hash_json(deletion.model_dump(mode="json")),
                            received_at=received_at,
                            recording_method=None,
                            record_uid=deletion.record_id,
                            request_id=batch.request_id,
                            start_time=None,
                            start_zone_offset_seconds=None,
                        )
                    )
                )
            elif deletion.record_type == "sleep":
                latest = await transaction.fetch_one_or_none(
                    select(HcSleepSession)
                    .where(HcSleepSession.record_uid.eq(deletion.record_id))
                    .order_by(HcSleepSession.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.is_deleted:
                    skipped["sleep"] += 1
                    continue
                _ = await transaction.execute(
                    insert(
                        HcSleepSession(
                            client_record_id=None,
                            client_record_version=None,
                            end_time=None,
                            end_zone_offset_seconds=None,
                            is_deleted=True,
                            modified_at=None,
                            notes=None,
                            origin_id=None if latest is None else latest.origin_id,
                            payload_hash=_hash_json(deletion.model_dump(mode="json")),
                            received_at=received_at,
                            recording_method=None,
                            record_uid=deletion.record_id,
                            request_id=batch.request_id,
                            start_time=None,
                            start_zone_offset_seconds=None,
                            title=None,
                        )
                    )
                )
            elif deletion.record_type == "steps":
                latest = await transaction.fetch_one_or_none(
                    select(HcStepInterval)
                    .where(HcStepInterval.record_uid.eq(deletion.record_id))
                    .order_by(HcStepInterval.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.is_deleted:
                    skipped["steps"] += 1
                    continue
                _ = await transaction.execute(
                    insert(
                        HcStepInterval(
                            client_record_id=None,
                            client_record_version=None,
                            count=None,
                            end_time=None,
                            end_zone_offset_seconds=None,
                            is_deleted=True,
                            modified_at=None,
                            origin_id=None if latest is None else latest.origin_id,
                            payload_hash=_hash_json(deletion.model_dump(mode="json")),
                            received_at=received_at,
                            recording_method=None,
                            record_uid=deletion.record_id,
                            request_id=batch.request_id,
                            start_time=None,
                            start_zone_offset_seconds=None,
                        )
                    )
                )
            else:
                latest = await transaction.fetch_one_or_none(
                    select(HcExerciseSession)
                    .where(HcExerciseSession.record_uid.eq(deletion.record_id))
                    .order_by(HcExerciseSession.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.is_deleted:
                    skipped["exercise"] += 1
                    continue
                _ = await transaction.execute(
                    insert(
                        HcExerciseSession(
                            client_record_id=None,
                            client_record_version=None,
                            end_time=None,
                            end_zone_offset_seconds=None,
                            exercise_type=None,
                            is_deleted=True,
                            modified_at=None,
                            notes=None,
                            origin_id=None if latest is None else latest.origin_id,
                            payload_hash=_hash_json(deletion.model_dump(mode="json")),
                            planned_exercise_session_id=None,
                            received_at=received_at,
                            recording_method=None,
                            record_uid=deletion.record_id,
                            request_id=batch.request_id,
                            start_time=None,
                            start_zone_offset_seconds=None,
                            title=None,
                        )
                    )
                )
            deleted[deletion.record_type] += 1

    async def _append_records(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        await self._append_heart_rates(
            transaction, batch, received_at, accepted, skipped
        )
        await self._append_sleep(transaction, batch, received_at, accepted, skipped)
        await self._append_steps(transaction, batch, received_at, accepted, skipped)
        await self._append_exercise(transaction, batch, received_at, accepted, skipped)

    async def _append_heart_rates(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.heart_rate:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcHeartRateRecord)
                .where(HcHeartRateRecord.record_uid.eq(record.metadata.id))
                .order_by(HcHeartRateRecord.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["heart_rate"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcHeartRateRecord(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                    )
                ).returning()
            )
            for index, sample in enumerate(record.samples):
                _ = await transaction.execute(
                    insert(
                        HcHeartRateSample(
                            version_id=parent.version_id,
                            sample_index=index,
                            time=sample.time,
                            beats_per_minute=sample.beats_per_minute,
                        )
                    )
                )
            accepted["heart_rate"] += 1

    async def _append_sleep(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.sleep:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcSleepSession)
                .where(HcSleepSession.record_uid.eq(record.metadata.id))
                .order_by(HcSleepSession.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["sleep"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcSleepSession(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        title=record.title,
                        notes=record.notes,
                    )
                ).returning()
            )
            for index, stage in enumerate(record.stages):
                _ = await transaction.execute(
                    insert(
                        HcSleepStage(
                            version_id=parent.version_id,
                            stage_index=index,
                            start_time=stage.start_time,
                            end_time=stage.end_time,
                            stage=stage.stage,
                        )
                    )
                )
            accepted["sleep"] += 1

    async def _append_steps(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.steps:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcStepInterval)
                .where(HcStepInterval.record_uid.eq(record.metadata.id))
                .order_by(HcStepInterval.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["steps"] += 1
                continue
            metadata = record.metadata
            _ = await transaction.execute(
                insert(
                    HcStepInterval(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        count=record.count,
                    )
                )
            )
            accepted["steps"] += 1

    async def _append_exercise(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.exercise:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcExerciseSession)
                .where(HcExerciseSession.record_uid.eq(record.metadata.id))
                .order_by(HcExerciseSession.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["exercise"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcExerciseSession(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        exercise_type=record.exercise_type,
                        title=record.title,
                        notes=record.notes,
                        planned_exercise_session_id=record.planned_exercise_session_id,
                    )
                ).returning()
            )
            for index, segment in enumerate(record.segments):
                _ = await transaction.execute(
                    insert(
                        HcExerciseSegment(
                            version_id=parent.version_id,
                            segment_index=index,
                            start_time=segment.start_time,
                            end_time=segment.end_time,
                            segment_type=segment.segment_type,
                            repetitions_count=segment.repetitions_count,
                        )
                    )
                )
            for index, lap in enumerate(record.laps):
                _ = await transaction.execute(
                    insert(
                        HcExerciseLap(
                            version_id=parent.version_id,
                            lap_index=index,
                            start_time=lap.start_time,
                            end_time=lap.end_time,
                            length_meters=lap.length_meters,
                        )
                    )
                )
            for index, point in enumerate(record.route):
                _ = await transaction.execute(
                    insert(
                        HcExerciseRoutePoint(
                            version_id=parent.version_id,
                            point_index=index,
                            time=point.time,
                            latitude=point.latitude,
                            longitude=point.longitude,
                            horizontal_accuracy_meters=point.horizontal_accuracy_meters,
                            vertical_accuracy_meters=point.vertical_accuracy_meters,
                            altitude_meters=point.altitude_meters,
                        )
                    )
                )
            accepted["exercise"] += 1


_CURRENT_VIEW_MIGRATIONS = {
    "hc_heart_rate_record_current": 'CREATE VIEW "hc_heart_rate_record_current" AS SELECT parent.* FROM "hc_heart_rate_record" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_heart_rate_record" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_sleep_session_current": 'CREATE VIEW "hc_sleep_session_current" AS SELECT parent.* FROM "hc_sleep_session" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_sleep_session" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_step_interval_current": 'CREATE VIEW "hc_step_interval_current" AS SELECT parent.* FROM "hc_step_interval" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_step_interval" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_exercise_session_current": 'CREATE VIEW "hc_exercise_session_current" AS SELECT parent.* FROM "hc_exercise_session" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_exercise_session" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
}


async def create_health_connect_schema(database: Database) -> None:
    """Initialize every typed table, index, and current-version view."""
    statements = scaffold(_MODELS).splitlines()
    migrations = {
        f"{index:04d}_health_connect_schema": sql
        for index, sql in enumerate(statements, start=1)
    }
    next_index = len(migrations) + 1
    for view, sql in _CURRENT_VIEW_MIGRATIONS.items():
        migrations[f"{next_index:04d}_{view}"] = sql
        next_index += 1
    child_views = {
        "hc_heart_rate_sample_current": 'SELECT child.* FROM "hc_heart_rate_sample" child JOIN "hc_heart_rate_record_current" parent ON parent."version_id" = child."version_id"',
        "hc_sleep_stage_current": 'SELECT child.* FROM "hc_sleep_stage" child JOIN "hc_sleep_session_current" parent ON parent."version_id" = child."version_id"',
        "hc_exercise_segment_current": 'SELECT child.* FROM "hc_exercise_segment" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
        "hc_exercise_lap_current": 'SELECT child.* FROM "hc_exercise_lap" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
        "hc_exercise_route_point_current": 'SELECT child.* FROM "hc_exercise_route_point" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
    }
    for view, query in child_views.items():
        migrations[f"{next_index:04d}_{view}"] = f'CREATE VIEW "{view}" AS {query}'
        next_index += 1
    await database.migrate(migrations)
    await database.verify(_MODELS)


@endpoint(query=HealthConnectSyncStateQuery, response=HealthConnectSyncStateRead)
async def read_health_connect_sync_state(
    request: Request, query: HealthConnectSyncStateQuery
) -> Response:
    try:
        record_types = _parse_record_types(query.record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    return JSONResponse(
        (
            await service.fetch_sync_state(query.installation_id, record_types)
        ).model_dump(mode="json")
    )


@endpoint(
    request_body=StartHealthConnectBaselineRequest,
    response=HealthConnectSyncStateRead,
    status=201,
)
async def start_health_connect_baseline(
    request: Request, body: StartHealthConnectBaselineRequest
) -> Response:
    try:
        record_types = _canonical_record_types(list(body.record_types))
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    state = await service.start_baseline(
        installation_id=body.installation_id,
        record_types=record_types,
        starting_token=body.starting_token,
        request_id=body.request_id,
    )
    cast("Logger", request.app.state.logger).info(
        "Health Connect baseline started",
        baseline_generation=state.baseline_generation,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(state.model_dump(mode="json"), status_code=201)


@endpoint(
    request_body=CompleteHealthConnectBaselineRequest,
    response=HealthConnectBaselineCompletionRead,
)
async def complete_health_connect_baseline(
    request: Request, body: CompleteHealthConnectBaselineRequest
) -> Response:
    """Reconcile bounded baseline absence and unlock live change pages."""
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    try:
        report = await service.complete_baseline(body)
    except HealthConnectCursorConflictError:
        cast("Logger", request.app.state.logger).warning(
            "Health Connect baseline completion conflicted",
            error_category="cursor_conflict",
            installation_id=body.installation_id,
            request_id=body.request_id,
        )
        return JSONResponse({"detail": "baseline state is stale"}, status_code=409)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    cast("Logger", request.app.state.logger).info(
        "Health Connect baseline completed",
        deleted=report.deleted,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(report.model_dump(mode="json"))


@endpoint(request_body=HealthConnectBatchRequest, response=HealthConnectBatchRead)
async def ingest_health_connect_batch(
    request: Request, body: HealthConnectBatchRequest
) -> Response:
    service = cast("HealthConnectService", request.app.state.health_connect_service)
    try:
        report = await service.ingest_batch(body)
    except HealthConnectCursorConflictError:
        cast("Logger", request.app.state.logger).warning(
            "Health Connect page conflicted",
            error_category="cursor_conflict",
            installation_id=body.installation_id,
            request_id=body.request_id,
        )
        return JSONResponse({"detail": "expected token is stale"}, status_code=409)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)
    cast("Logger", request.app.state.logger).info(
        "Health Connect page accepted",
        accepted=report.accepted,
        deleted=report.deleted,
        installation_id=body.installation_id,
        replayed=report.replayed,
        request_id=body.request_id,
        skipped=report.skipped,
    )
    return JSONResponse(report.model_dump(mode="json"))


health_connect_routes: list[Route] = [
    EndpointRoute(
        "/api/telemetry/health-connect/sync-state/baselines",
        start_health_connect_baseline,
        methods=["POST"],
    ),
    EndpointRoute(
        "/api/telemetry/health-connect/sync-state/baselines/complete",
        complete_health_connect_baseline,
        methods=["POST"],
    ),
    EndpointRoute(
        "/api/telemetry/health-connect/sync-state",
        read_health_connect_sync_state,
        methods=["GET"],
    ),
    EndpointRoute(
        "/api/telemetry/health-connect/batches",
        ingest_health_connect_batch,
        methods=["POST"],
    ),
]
