"""Typed, append-only Health Connect telemetry store and sync HTTP contract."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    delete,
    insert,
    not_exists,
    scaffold,
    select,
    update,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger
from tether.openapi import EndpointRoute, endpoint

HealthRecordType = Literal[
    "active_calories_burned",
    "basal_body_temperature",
    "basal_metabolic_rate",
    "blood_glucose",
    "blood_pressure",
    "body_fat",
    "body_temperature",
    "body_water_mass",
    "bone_mass",
    "cervical_mucus",
    "cycling_pedaling_cadence",
    "distance",
    "elevation_gained",
    "exercise",
    "floors_climbed",
    "heart_rate",
    "heart_rate_variability_rmssd",
    "height",
    "hydration",
    "intermenstrual_bleeding",
    "lean_body_mass",
    "menstruation_flow",
    "menstruation_period",
    "mindfulness_session",
    "nutrition",
    "ovulation_test",
    "oxygen_saturation",
    "planned_exercise_session",
    "power",
    "respiratory_rate",
    "resting_heart_rate",
    "sexual_activity",
    "skin_temperature",
    "sleep",
    "speed",
    "steps",
    "steps_cadence",
    "total_calories_burned",
    "vo2_max",
    "weight",
    "wheelchair_pushes",
]
_ALL_RECORD_TYPES: tuple[HealthRecordType, ...] = (
    "active_calories_burned",
    "basal_body_temperature",
    "basal_metabolic_rate",
    "blood_glucose",
    "blood_pressure",
    "body_fat",
    "body_temperature",
    "body_water_mass",
    "bone_mass",
    "cervical_mucus",
    "cycling_pedaling_cadence",
    "distance",
    "elevation_gained",
    "exercise",
    "floors_climbed",
    "heart_rate",
    "heart_rate_variability_rmssd",
    "height",
    "hydration",
    "intermenstrual_bleeding",
    "lean_body_mass",
    "menstruation_flow",
    "menstruation_period",
    "mindfulness_session",
    "nutrition",
    "ovulation_test",
    "oxygen_saturation",
    "planned_exercise_session",
    "power",
    "respiratory_rate",
    "resting_heart_rate",
    "sexual_activity",
    "skin_temperature",
    "sleep",
    "speed",
    "steps",
    "steps_cadence",
    "total_calories_burned",
    "vo2_max",
    "weight",
    "wheelchair_pushes",
)
_CAPTURED_RECORD_TYPES = frozenset({"exercise", "heart_rate", "sleep", "steps"})
_ALLOWED_RECORD_TYPES = frozenset(_ALL_RECORD_TYPES)
_GENERIC_RECORD_TYPES: tuple[HealthRecordType, ...] = tuple(
    record_type
    for record_type in _ALL_RECORD_TYPES
    if record_type not in _CAPTURED_RECORD_TYPES
)
RecordStatus = Literal["baseline", "changes", "initial"]
GENERIC_RECORD_CONTRACT_VERSION = 3


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


class HcBaselineSeen[S = Pending](Model[S, "HcBaselineSeen[Fetched]"]):
    """One record observed during one uploaded baseline page."""

    seen_key: HcBaselineSeen.Col[str] = Text(primary_key=True)
    state_key: HcBaselineSeen.Col[str] = Text(nullable=False, index=True)
    baseline_generation: HcBaselineSeen.Col[int] = Integer(nullable=False)
    record_type: HcBaselineSeen.Col[str] = Text(nullable=False)
    record_uid: HcBaselineSeen.Col[str] = Text(nullable=False, index=True)


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


class HcGenericRecord[S = Pending](Model[S, "HcGenericRecord[Fetched]"]):
    """Append-only raw storage for expanded Health Connect record types."""

    version_id: HcGenericRecord.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_type: HcGenericRecord.Col[str] = Text(nullable=False, index=True)
    record_uid: HcGenericRecord.Col[str] = Text(nullable=False, index=True)
    origin_id: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    modified_at: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    received_at: HcGenericRecord.Col[int] = Integer(nullable=False)
    request_id: HcGenericRecord.Col[str] = Text(nullable=False, index=True)
    is_deleted: HcGenericRecord.Col[bool] = Integer(nullable=False)
    payload_hash: HcGenericRecord.Col[str] = Text(nullable=False)
    client_record_id: HcGenericRecord.Col[str | None] = Text(nullable=True)
    client_record_version: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    recording_method: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    start_time: HcGenericRecord.Col[int | None] = Integer(nullable=True, index=True)
    end_time: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: HcGenericRecord.Col[int | None] = Integer(nullable=True)
    payload_json: HcGenericRecord.Col[str | None] = Text(nullable=True)


class HcHeartRateRecordCurrent[S = Pending](
    Model[S, "HcHeartRateRecordCurrent[Fetched]"]
):
    version_id: HcHeartRateRecordCurrent.Col[int] = Integer(primary_key=True)
    record_uid: HcHeartRateRecordCurrent.Col[str] = Text(nullable=False)
    origin_id: HcHeartRateRecordCurrent.Col[int | None] = Integer(nullable=True)
    modified_at: HcHeartRateRecordCurrent.Col[int | None] = Integer(nullable=True)
    received_at: HcHeartRateRecordCurrent.Col[int] = Integer(nullable=False)
    client_record_id: HcHeartRateRecordCurrent.Col[str | None] = Text(nullable=True)
    client_record_version: HcHeartRateRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )
    recording_method: HcHeartRateRecordCurrent.Col[int | None] = Integer(nullable=True)
    start_time: HcHeartRateRecordCurrent.Col[int | None] = Integer(nullable=True)
    end_time: HcHeartRateRecordCurrent.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcHeartRateRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcHeartRateRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )


class HcSleepSessionCurrent[S = Pending](Model[S, "HcSleepSessionCurrent[Fetched]"]):
    version_id: HcSleepSessionCurrent.Col[int] = Integer(primary_key=True)
    record_uid: HcSleepSessionCurrent.Col[str] = Text(nullable=False)
    origin_id: HcSleepSessionCurrent.Col[int | None] = Integer(nullable=True)
    modified_at: HcSleepSessionCurrent.Col[int | None] = Integer(nullable=True)
    received_at: HcSleepSessionCurrent.Col[int] = Integer(nullable=False)
    client_record_id: HcSleepSessionCurrent.Col[str | None] = Text(nullable=True)
    client_record_version: HcSleepSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    recording_method: HcSleepSessionCurrent.Col[int | None] = Integer(nullable=True)
    start_time: HcSleepSessionCurrent.Col[int | None] = Integer(nullable=True)
    end_time: HcSleepSessionCurrent.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcSleepSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcSleepSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    title: HcSleepSessionCurrent.Col[str | None] = Text(nullable=True)
    notes: HcSleepSessionCurrent.Col[str | None] = Text(nullable=True)


class HcStepIntervalCurrent[S = Pending](Model[S, "HcStepIntervalCurrent[Fetched]"]):
    version_id: HcStepIntervalCurrent.Col[int] = Integer(primary_key=True)
    record_uid: HcStepIntervalCurrent.Col[str] = Text(nullable=False)
    origin_id: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)
    modified_at: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)
    received_at: HcStepIntervalCurrent.Col[int] = Integer(nullable=False)
    client_record_id: HcStepIntervalCurrent.Col[str | None] = Text(nullable=True)
    client_record_version: HcStepIntervalCurrent.Col[int | None] = Integer(
        nullable=True
    )
    recording_method: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)
    start_time: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)
    end_time: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcStepIntervalCurrent.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcStepIntervalCurrent.Col[int | None] = Integer(
        nullable=True
    )
    count: HcStepIntervalCurrent.Col[int | None] = Integer(nullable=True)


class HcExerciseSessionCurrent[S = Pending](
    Model[S, "HcExerciseSessionCurrent[Fetched]"]
):
    version_id: HcExerciseSessionCurrent.Col[int] = Integer(primary_key=True)
    record_uid: HcExerciseSessionCurrent.Col[str] = Text(nullable=False)
    origin_id: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    modified_at: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    received_at: HcExerciseSessionCurrent.Col[int] = Integer(nullable=False)
    client_record_id: HcExerciseSessionCurrent.Col[str | None] = Text(nullable=True)
    client_record_version: HcExerciseSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    recording_method: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    start_time: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    end_time: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcExerciseSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcExerciseSessionCurrent.Col[int | None] = Integer(
        nullable=True
    )
    exercise_type: HcExerciseSessionCurrent.Col[int | None] = Integer(nullable=True)
    title: HcExerciseSessionCurrent.Col[str | None] = Text(nullable=True)
    notes: HcExerciseSessionCurrent.Col[str | None] = Text(nullable=True)
    planned_exercise_session_id: HcExerciseSessionCurrent.Col[str | None] = Text(
        nullable=True
    )


class HcGenericRecordCurrent[S = Pending](Model[S, "HcGenericRecordCurrent[Fetched]"]):
    version_id: HcGenericRecordCurrent.Col[int] = Integer(primary_key=True)
    record_type: HcGenericRecordCurrent.Col[str] = Text(nullable=False)
    record_uid: HcGenericRecordCurrent.Col[str] = Text(nullable=False)
    origin_id: HcGenericRecordCurrent.Col[int | None] = Integer(nullable=True)
    modified_at: HcGenericRecordCurrent.Col[int | None] = Integer(nullable=True)
    received_at: HcGenericRecordCurrent.Col[int] = Integer(nullable=False)
    client_record_id: HcGenericRecordCurrent.Col[str | None] = Text(nullable=True)
    client_record_version: HcGenericRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )
    recording_method: HcGenericRecordCurrent.Col[int | None] = Integer(nullable=True)
    start_time: HcGenericRecordCurrent.Col[int | None] = Integer(nullable=True)
    end_time: HcGenericRecordCurrent.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: HcGenericRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )
    end_zone_offset_seconds: HcGenericRecordCurrent.Col[int | None] = Integer(
        nullable=True
    )
    payload_json: HcGenericRecordCurrent.Col[str | None] = Text(nullable=True)


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


class GenericRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int | None = None
    end_time: int | None = None
    start_zone_offset_seconds: int | None = None
    end_zone_offset_seconds: int | None = None
    payload: dict[str, object] = Field(default_factory=dict)


def _generic_record_list_field() -> list[GenericRecord]:
    return cast("list[GenericRecord]", Field(default_factory=list, max_length=1_000))


def _exercise_record_list_field() -> list[ExerciseRecord]:
    return cast("list[ExerciseRecord]", Field(default_factory=list, max_length=1_000))


def _heart_rate_record_list_field() -> list[HeartRateRecord]:
    return cast("list[HeartRateRecord]", Field(default_factory=list, max_length=1_000))


def _sleep_record_list_field() -> list[SleepRecord]:
    return cast("list[SleepRecord]", Field(default_factory=list, max_length=1_000))


def _steps_record_list_field() -> list[StepsRecord]:
    return cast("list[StepsRecord]", Field(default_factory=list, max_length=1_000))


class HealthConnectRecords(HealthConnectWireModel):
    active_calories_burned: list[GenericRecord] = _generic_record_list_field()
    basal_body_temperature: list[GenericRecord] = _generic_record_list_field()
    basal_metabolic_rate: list[GenericRecord] = _generic_record_list_field()
    blood_glucose: list[GenericRecord] = _generic_record_list_field()
    blood_pressure: list[GenericRecord] = _generic_record_list_field()
    body_fat: list[GenericRecord] = _generic_record_list_field()
    body_temperature: list[GenericRecord] = _generic_record_list_field()
    body_water_mass: list[GenericRecord] = _generic_record_list_field()
    bone_mass: list[GenericRecord] = _generic_record_list_field()
    cervical_mucus: list[GenericRecord] = _generic_record_list_field()
    cycling_pedaling_cadence: list[GenericRecord] = _generic_record_list_field()
    distance: list[GenericRecord] = _generic_record_list_field()
    elevation_gained: list[GenericRecord] = _generic_record_list_field()
    exercise: list[ExerciseRecord] = _exercise_record_list_field()
    floors_climbed: list[GenericRecord] = _generic_record_list_field()
    heart_rate: list[HeartRateRecord] = _heart_rate_record_list_field()
    heart_rate_variability_rmssd: list[GenericRecord] = _generic_record_list_field()
    height: list[GenericRecord] = _generic_record_list_field()
    hydration: list[GenericRecord] = _generic_record_list_field()
    intermenstrual_bleeding: list[GenericRecord] = _generic_record_list_field()
    lean_body_mass: list[GenericRecord] = _generic_record_list_field()
    menstruation_flow: list[GenericRecord] = _generic_record_list_field()
    menstruation_period: list[GenericRecord] = _generic_record_list_field()
    mindfulness_session: list[GenericRecord] = _generic_record_list_field()
    nutrition: list[GenericRecord] = _generic_record_list_field()
    ovulation_test: list[GenericRecord] = _generic_record_list_field()
    oxygen_saturation: list[GenericRecord] = _generic_record_list_field()
    planned_exercise_session: list[GenericRecord] = _generic_record_list_field()
    power: list[GenericRecord] = _generic_record_list_field()
    respiratory_rate: list[GenericRecord] = _generic_record_list_field()
    resting_heart_rate: list[GenericRecord] = _generic_record_list_field()
    sexual_activity: list[GenericRecord] = _generic_record_list_field()
    skin_temperature: list[GenericRecord] = _generic_record_list_field()
    sleep: list[SleepRecord] = _sleep_record_list_field()
    speed: list[GenericRecord] = _generic_record_list_field()
    steps: list[StepsRecord] = _steps_record_list_field()
    steps_cadence: list[GenericRecord] = _generic_record_list_field()
    total_calories_burned: list[GenericRecord] = _generic_record_list_field()
    vo2_max: list[GenericRecord] = _generic_record_list_field()
    weight: list[GenericRecord] = _generic_record_list_field()
    wheelchair_pushes: list[GenericRecord] = _generic_record_list_field()


class HealthConnectDeletion(HealthConnectWireModel):
    record_type: HealthRecordType
    record_id: str = Field(min_length=1)


class HealthConnectBatchRequest(HealthConnectWireModel):
    contract_version: Literal[1, 2, 3]
    mode: Literal["baseline", "changes"]
    installation_id: str = Field(min_length=1)
    record_types: list[HealthRecordType]
    request_id: str = Field(min_length=1)
    expected_token: str
    next_token: str
    records: HealthConnectRecords
    deletions: list[HealthConnectDeletion] = Field(max_length=10_000)


class AuthoritativeScanRange(HealthConnectWireModel):
    """Exact time range scanned authoritatively by Health Connect."""

    start_time: int
    end_time: int
    seen_record_ids: list[str] | None = Field(default=None, max_length=100_000)


class V1SeenIdsRequiredError(ValueError):
    """A v1 completion omitted its authoritative client ID set."""

    def __init__(self) -> None:
        super().__init__("contract v1 completion requires seen_record_ids")


class BaselineRangesMismatchError(ValueError):
    """Baseline completion ranges do not match the stream's record types."""

    def __init__(self) -> None:
        super().__init__("ranges must match record_types")


class CompleteHealthConnectBaselineRequest(HealthConnectWireModel):
    """Bounded authoritative scan used to reconcile expired-token gaps."""

    contract_version: Literal[1, 2, 3]
    installation_id: str
    record_types: list[HealthRecordType]
    request_id: str
    expected_token: str
    baseline_generation: int = Field(gt=0)
    ranges: dict[HealthRecordType, AuthoritativeScanRange]

    @model_validator(mode="after")
    def ranges_match_record_types(self) -> Self:
        """Reconcile only streams explicitly granted and scanned by Android."""
        record_types = set(_canonical_record_types(list(self.record_types)))
        if set(self.ranges) != record_types:
            raise BaselineRangesMismatchError
        if self.contract_version == 1 and any(
            scan.seen_record_ids is None for scan in self.ranges.values()
        ):
            raise V1SeenIdsRequiredError
        return self


class HealthConnectBaselineCompletionRead(HealthConnectWireModel):
    """Safe operational counts from baseline reconciliation."""

    deleted: dict[HealthRecordType, int]
    status: Literal["completed"]


class HealthConnectSyncStateQuery(HealthConnectWireModel):
    installation_id: str
    record_types: str


class StartHealthConnectBaselineRequest(HealthConnectWireModel):
    contract_version: Literal[1, 2, 3]
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


_LEGACY_SCHEMA_MODELS = [
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
_SCHEMA_MODELS = [*_LEGACY_SCHEMA_MODELS, HcGenericRecord]
_MODELS = [*_SCHEMA_MODELS, HcBaselineSeen]
_PARENT_MODELS = {
    "exercise": HcExerciseSession,
    "heart_rate": HcHeartRateRecord,
    "sleep": HcSleepSession,
    "steps": HcStepInterval,
}


def _empty_counts(
    record_types: tuple[HealthRecordType, ...],
) -> dict[HealthRecordType, int]:
    return dict.fromkeys(record_types, 0)


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


def _validate_versioned_record_types(
    contract_version: int,
    record_types: tuple[HealthRecordType, ...],
) -> None:
    if (
        contract_version < GENERIC_RECORD_CONTRACT_VERSION
        and not set(record_types) <= _CAPTURED_RECORD_TYPES
    ):
        raise UnsupportedRecordTypesError


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
class HealthConnectIngestion:
    """Atomic Health Connect cursor, baseline, replay, and append gate.

    Example:
        ingestion = HealthConnectIngestion(database)
        state = await ingestion.fetch_sync_state("phone", ("steps",))
    """

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
            _ = await transaction.execute(
                delete(HcBaselineSeen).where(HcBaselineSeen.state_key.eq(key))
            )
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
        _validate_versioned_record_types(body.contract_version, record_types)
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
            deleted = await self._reconcile_baseline(
                transaction, body, key, record_types
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

    async def _reconcile_baseline(
        self,
        transaction: Transaction,
        body: CompleteHealthConnectBaselineRequest,
        key: str,
        record_types: tuple[HealthRecordType, ...],
    ) -> dict[HealthRecordType, int]:
        """Tombstone missing current records in bounded batches."""
        if body.contract_version == 1:
            for record_type, scan in body.ranges.items():
                for record_uid in scan.seen_record_ids or []:
                    _ = await transaction.execute(
                        insert(
                            HcBaselineSeen(
                                seen_key=_hash_json(
                                    [body.request_id, record_type, record_uid]
                                ),
                                state_key=key,
                                baseline_generation=body.baseline_generation,
                                record_type=record_type,
                                record_uid=record_uid,
                            )
                        )
                    )
        deleted, skipped = _empty_counts(record_types), _empty_counts(record_types)
        cursors: dict[HealthRecordType, int] = dict.fromkeys(record_types, 0)
        while True:
            rows_by_type = await self._fetch_missing_current_rows(
                transaction, body, key, record_types, cursors
            )
            if not any(rows_by_type.values()):
                break
            for record_type, rows in rows_by_type.items():
                if rows:
                    cursors[record_type] = rows[-1].version_id
            reconciliation_batch = HealthConnectBatchRequest(
                contract_version=1,
                deletions=[
                    HealthConnectDeletion(
                        record_type=record_type, record_id=row.record_uid
                    )
                    for record_type, rows in rows_by_type.items()
                    for row in rows
                ],
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
            delete(HcBaselineSeen).where(HcBaselineSeen.state_key.eq(key))
        )
        return deleted

    async def _fetch_missing_current_rows(
        self,
        transaction: Transaction,
        body: CompleteHealthConnectBaselineRequest,
        key: str,
        record_types: tuple[HealthRecordType, ...],
        cursors: dict[HealthRecordType, int],
    ) -> dict[HealthRecordType, list[Any]]:
        rows: dict[HealthRecordType, list[Any]] = {}
        if "heart_rate" in record_types:
            scan = body.ranges["heart_rate"]
            rows["heart_rate"] = await transaction.fetch_all(
                select(HcHeartRateRecordCurrent)
                .where(HcHeartRateRecordCurrent.version_id.gt(cursors["heart_rate"]))
                .where(HcHeartRateRecordCurrent.start_time.gte(scan.start_time))
                .where(HcHeartRateRecordCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("heart_rate"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcHeartRateRecordCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcHeartRateRecordCurrent.version_id.asc())
                .limit(500)
            )
        if "sleep" in record_types:
            scan = body.ranges["sleep"]
            rows["sleep"] = await transaction.fetch_all(
                select(HcSleepSessionCurrent)
                .where(HcSleepSessionCurrent.version_id.gt(cursors["sleep"]))
                .where(HcSleepSessionCurrent.start_time.gte(scan.start_time))
                .where(HcSleepSessionCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("sleep"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcSleepSessionCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcSleepSessionCurrent.version_id.asc())
                .limit(500)
            )
        if "steps" in record_types:
            scan = body.ranges["steps"]
            rows["steps"] = await transaction.fetch_all(
                select(HcStepIntervalCurrent)
                .where(HcStepIntervalCurrent.version_id.gt(cursors["steps"]))
                .where(HcStepIntervalCurrent.start_time.gte(scan.start_time))
                .where(HcStepIntervalCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("steps"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcStepIntervalCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcStepIntervalCurrent.version_id.asc())
                .limit(500)
            )
        if "exercise" in record_types:
            scan = body.ranges["exercise"]
            rows["exercise"] = await transaction.fetch_all(
                select(HcExerciseSessionCurrent)
                .where(HcExerciseSessionCurrent.version_id.gt(cursors["exercise"]))
                .where(HcExerciseSessionCurrent.start_time.gte(scan.start_time))
                .where(HcExerciseSessionCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("exercise"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcExerciseSessionCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcExerciseSessionCurrent.version_id.asc())
                .limit(500)
            )
        return rows

    async def ingest_batch(
        self, batch: HealthConnectBatchRequest
    ) -> HealthConnectBatchRead:
        record_types = _canonical_record_types(list(batch.record_types))
        _validate_versioned_record_types(batch.contract_version, record_types)
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
                _empty_counts(record_types),
                _empty_counts(record_types),
                _empty_counts(record_types),
            )
            received_at = time.time_ns() // 1_000_000
            await self._append_records(
                transaction, batch, received_at, accepted, skipped
            )
            if batch.mode == "baseline":
                baseline_records = [
                    ("exercise", batch.records.exercise),
                    ("heart_rate", batch.records.heart_rate),
                    ("sleep", batch.records.sleep),
                    ("steps", batch.records.steps),
                    *(
                        (record_type, getattr(batch.records, record_type))
                        for record_type in _GENERIC_RECORD_TYPES
                    ),
                ]
                for record_type, records in baseline_records:
                    for record in records:
                        _ = await transaction.execute(
                            insert(
                                HcBaselineSeen(
                                    seen_key=_hash_json(
                                        [
                                            batch.request_id,
                                            record_type,
                                            record.metadata.id,
                                        ]
                                    ),
                                    state_key=key,
                                    baseline_generation=state.baseline_generation,
                                    record_type=record_type,
                                    record_uid=record.metadata.id,
                                )
                            )
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
        appenders = {
            "exercise": self._append_exercise_deletion,
            "heart_rate": self._append_heart_rate_deletion,
            "sleep": self._append_sleep_deletion,
            "steps": self._append_steps_deletion,
        }
        for deletion in batch.deletions:
            appender = appenders.get(
                deletion.record_type, self._append_generic_deletion
            )
            if await appender(transaction, batch, deletion, received_at, skipped):
                deleted[deletion.record_type] += 1

    async def _append_heart_rate_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcHeartRateRecord)
            .where(HcHeartRateRecord.record_uid.eq(deletion.record_id))
            .order_by(HcHeartRateRecord.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["heart_rate"] += 1
            return False
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
        return True

    async def _append_sleep_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcSleepSession)
            .where(HcSleepSession.record_uid.eq(deletion.record_id))
            .order_by(HcSleepSession.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["sleep"] += 1
            return False
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
        return True

    async def _append_steps_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcStepInterval)
            .where(HcStepInterval.record_uid.eq(deletion.record_id))
            .order_by(HcStepInterval.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["steps"] += 1
            return False
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
        return True

    async def _append_exercise_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcExerciseSession)
            .where(HcExerciseSession.record_uid.eq(deletion.record_id))
            .order_by(HcExerciseSession.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["exercise"] += 1
            return False
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
        return True

    async def _append_generic_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcGenericRecord)
            .where(HcGenericRecord.record_type.eq(deletion.record_type))
            .where(HcGenericRecord.record_uid.eq(deletion.record_id))
            .order_by(HcGenericRecord.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped[deletion.record_type] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcGenericRecord(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    is_deleted=True,
                    modified_at=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    payload_json=None,
                    received_at=received_at,
                    recording_method=None,
                    record_type=deletion.record_type,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                )
            )
        )
        return True

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
        await self._append_generic(transaction, batch, received_at, accepted, skipped)

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

    async def _append_generic(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record_type in _GENERIC_RECORD_TYPES:
            for record in getattr(batch.records, record_type):
                digest = _hash_json(record.model_dump(mode="json"))
                latest = await transaction.fetch_one_or_none(
                    select(HcGenericRecord)
                    .where(HcGenericRecord.record_type.eq(record_type))
                    .where(HcGenericRecord.record_uid.eq(record.metadata.id))
                    .order_by(HcGenericRecord.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.payload_hash == digest:
                    skipped[record_type] += 1
                    continue
                metadata = record.metadata
                _ = await transaction.execute(
                    insert(
                        HcGenericRecord(
                            record_type=record_type,
                            record_uid=metadata.id,
                            origin_id=await _origin_id(transaction, metadata),
                            modified_at=metadata.last_modified_time,
                            received_at=received_at,
                            request_id=batch.request_id,
                            is_deleted=False,
                            payload_hash=digest,
                            payload_json=json.dumps(
                                record.payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            client_record_id=metadata.client_record_id,
                            client_record_version=metadata.client_record_version,
                            recording_method=metadata.recording_method,
                            start_time=record.start_time,
                            end_time=record.end_time,
                            start_zone_offset_seconds=record.start_zone_offset_seconds,
                            end_zone_offset_seconds=record.end_zone_offset_seconds,
                        )
                    )
                )
                accepted[record_type] += 1


_CURRENT_VIEW_MIGRATIONS = {
    "hc_heart_rate_record_current": 'CREATE VIEW "hc_heart_rate_record_current" AS SELECT parent.* FROM "hc_heart_rate_record" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_heart_rate_record" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_sleep_session_current": 'CREATE VIEW "hc_sleep_session_current" AS SELECT parent.* FROM "hc_sleep_session" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_sleep_session" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_step_interval_current": 'CREATE VIEW "hc_step_interval_current" AS SELECT parent.* FROM "hc_step_interval" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_step_interval" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_exercise_session_current": 'CREATE VIEW "hc_exercise_session_current" AS SELECT parent.* FROM "hc_exercise_session" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_exercise_session" candidate WHERE candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
    "hc_generic_record_current": 'CREATE VIEW "hc_generic_record_current" AS SELECT parent.* FROM "hc_generic_record" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_generic_record" candidate WHERE candidate."record_type" = parent."record_type" AND candidate."record_uid" = parent."record_uid") AND parent."is_deleted" = 0',
}
_CHILD_CURRENT_VIEW_MIGRATIONS = {
    "hc_heart_rate_sample_current": 'SELECT child.* FROM "hc_heart_rate_sample" child JOIN "hc_heart_rate_record_current" parent ON parent."version_id" = child."version_id"',
    "hc_sleep_stage_current": 'SELECT child.* FROM "hc_sleep_stage" child JOIN "hc_sleep_session_current" parent ON parent."version_id" = child."version_id"',
    "hc_exercise_segment_current": 'SELECT child.* FROM "hc_exercise_segment" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
    "hc_exercise_lap_current": 'SELECT child.* FROM "hc_exercise_lap" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
    "hc_exercise_route_point_current": 'SELECT child.* FROM "hc_exercise_route_point" child JOIN "hc_exercise_session_current" parent ON parent."version_id" = child."version_id"',
}


def _create_if_not_exists(sql: str) -> str:
    """Make scaffold/view DDL safe when adopting a shifted migration key."""
    for prefix in (
        "CREATE UNIQUE INDEX ",
        "CREATE INDEX ",
        "CREATE TABLE ",
        "CREATE VIEW ",
    ):
        if sql.startswith(prefix):
            return sql.replace(prefix, f"{prefix}IF NOT EXISTS ", 1)
    return sql


async def create_health_connect_schema(database: Database) -> None:
    """Initialize every typed table, index, and current-version view."""
    # Freeze the original positional migration keys. Appending HcGenericRecord to
    # the combined scaffold shifted every later key and replayed existing views
    # on production. Future schema additions must likewise append explicit keys.
    legacy_statements = scaffold(_LEGACY_SCHEMA_MODELS).splitlines()
    migrations = {
        f"{index:04d}_health_connect_schema": sql
        for index, sql in enumerate(legacy_statements, start=1)
    }
    next_index = len(migrations) + 1
    for view, sql in _CURRENT_VIEW_MIGRATIONS.items():
        if view == "hc_generic_record_current":
            continue
        migrations[f"{next_index:04d}_{view}"] = _create_if_not_exists(sql)
        next_index += 1
    for view, query in _CHILD_CURRENT_VIEW_MIGRATIONS.items():
        sql = f'CREATE VIEW "{view}" AS {query}'
        migrations[f"{next_index:04d}_{view}"] = _create_if_not_exists(sql)
        next_index += 1
    for sql in scaffold([HcBaselineSeen]).splitlines():
        migrations[f"{next_index:04d}_baseline_seen"] = _create_if_not_exists(sql)
        next_index += 1

    # Preserve the five keys already applied by the failed v3 production boot.
    generic_start = len(legacy_statements) + 1
    for index, sql in enumerate(
        scaffold([HcGenericRecord]).splitlines(), start=generic_start
    ):
        migrations[f"{index:04d}_health_connect_schema"] = sql
    generic_view = _CURRENT_VIEW_MIGRATIONS["hc_generic_record_current"]
    migrations["0045_hc_generic_record_current"] = _create_if_not_exists(generic_view)

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
    service = cast("HealthConnectIngestion", request.app.state.health_connect_ingestion)
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
        _validate_versioned_record_types(body.contract_version, record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = cast("HealthConnectIngestion", request.app.state.health_connect_ingestion)
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
    service = cast("HealthConnectIngestion", request.app.state.health_connect_ingestion)
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
    service = cast("HealthConnectIngestion", request.app.state.health_connect_ingestion)
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
