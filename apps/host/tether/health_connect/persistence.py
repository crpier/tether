"""Canonical Health Connect SQLite models and byte-stable schema chain."""

from __future__ import annotations

from snekql import sqlite
from snekql.sqlite import (
    PENDING_GENERATION,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Real,
    Text,
    scaffold,
)


class HealthConnectSyncState[S = Pending](Model[S, "HealthConnectSyncState[Fetched]"]):
    """Durable cursor and baseline generation for one installation/type set."""

    __tablename__ = "hc_sync_state"
    state_key: sqlite.Col[str] = Text(primary_key=True)
    baseline_generation: sqlite.Col[int] = Integer(nullable=False)
    baseline_request_id: sqlite.Col[str | None] = Text(nullable=True)
    completion_deleted_json: sqlite.Col[str | None] = Text(nullable=True)
    completion_request_id: sqlite.Col[str | None] = Text(nullable=True)
    current_token: sqlite.Col[str | None] = Text(nullable=True)
    installation_id: sqlite.Col[str] = Text(nullable=False, index=True)
    record_type_set: sqlite.Col[str] = Text(nullable=False)
    status: sqlite.Col[str] = Text(nullable=False)


class HcOrigin[S = Pending](Model[S, "HcOrigin[Fetched]"]):
    """Writing application and nullable device provenance."""

    origin_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    origin_key: sqlite.Col[str] = Text(nullable=False, unique=True)
    data_origin_package: sqlite.Col[str] = Text(nullable=False)
    device_manufacturer: sqlite.Col[str | None] = Text(nullable=True)
    device_model: sqlite.Col[str | None] = Text(nullable=True)
    device_type: sqlite.Col[int | None] = Integer(nullable=True)


class HcPageRequest[S = Pending](Model[S, "HcPageRequest[Fetched]"]):
    """Committed request identity making response-loss retries idempotent."""

    request_id: sqlite.Col[str] = Text(primary_key=True)
    state_key: sqlite.Col[str] = Text(nullable=False, index=True)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    accepted_json: sqlite.Col[str] = Text(nullable=False)
    deleted_json: sqlite.Col[str] = Text(nullable=False)
    skipped_json: sqlite.Col[str] = Text(nullable=False)


class HcBaselineSeen[S = Pending](Model[S, "HcBaselineSeen[Fetched]"]):
    """One record observed during one uploaded baseline page."""

    seen_key: sqlite.Col[str] = Text(primary_key=True)
    state_key: sqlite.Col[str] = Text(nullable=False, index=True)
    baseline_generation: sqlite.Col[int] = Integer(nullable=False)
    record_type: sqlite.Col[str] = Text(nullable=False)
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)


class HcHeartRateRecord[S = Pending](Model[S, "HcHeartRateRecord[Fetched]"]):
    """One accepted heart-rate record version or tombstone."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True, index=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)


class HcHeartRateSample[S = Pending](Model[S, "HcHeartRateSample[Fetched]"]):
    """An ordered sample belonging to exactly one heart-rate version."""

    sample_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: sqlite.Col[int] = Integer(nullable=False, index=True)
    sample_index: sqlite.Col[int] = Integer(nullable=False)
    time: sqlite.Col[int] = Integer(nullable=False, index=True)
    beats_per_minute: sqlite.Col[int] = Integer(nullable=False)


class HcSleepSession[S = Pending](Model[S, "HcSleepSession[Fetched]"]):
    """One accepted sleep-session version or tombstone."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True, index=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    notes: sqlite.Col[str | None] = Text(nullable=True)


class HcSleepStage[S = Pending](Model[S, "HcSleepStage[Fetched]"]):
    """An ordered original-enum stage belonging to one sleep version."""

    stage_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: sqlite.Col[int] = Integer(nullable=False, index=True)
    stage_index: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False, index=True)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    stage: sqlite.Col[int] = Integer(nullable=False)


class HcStepInterval[S = Pending](Model[S, "HcStepInterval[Fetched]"]):
    """One accepted step interval version or tombstone."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True, index=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    count: sqlite.Col[int | None] = Integer(nullable=True)


class HcExerciseSession[S = Pending](Model[S, "HcExerciseSession[Fetched]"]):
    """One accepted exercise-session version or tombstone."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True, index=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    exercise_type: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    notes: sqlite.Col[str | None] = Text(nullable=True)
    planned_exercise_session_id: sqlite.Col[str | None] = Text(nullable=True)


class HcExerciseSegment[S = Pending](Model[S, "HcExerciseSegment[Fetched]"]):
    """An ordered segment belonging to one exercise version."""

    segment_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: sqlite.Col[int] = Integer(nullable=False, index=True)
    segment_index: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    segment_type: sqlite.Col[int] = Integer(nullable=False)
    repetitions_count: sqlite.Col[int] = Integer(nullable=False)


class HcExerciseLap[S = Pending](Model[S, "HcExerciseLap[Fetched]"]):
    """An ordered lap with canonical meter length."""

    lap_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: sqlite.Col[int] = Integer(nullable=False, index=True)
    lap_index: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    length_meters: sqlite.Col[float | None] = Real(nullable=True)


class HcExerciseRoutePoint[S = Pending](Model[S, "HcExerciseRoutePoint[Fetched]"]):
    """An ordered route point with Health Connect's canonical units."""

    route_point_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    version_id: sqlite.Col[int] = Integer(nullable=False, index=True)
    point_index: sqlite.Col[int] = Integer(nullable=False)
    time: sqlite.Col[int] = Integer(nullable=False, index=True)
    latitude: sqlite.Col[float] = Real(nullable=False)
    longitude: sqlite.Col[float] = Real(nullable=False)
    horizontal_accuracy_meters: sqlite.Col[float | None] = Real(nullable=True)
    vertical_accuracy_meters: sqlite.Col[float | None] = Real(nullable=True)
    altitude_meters: sqlite.Col[float | None] = Real(nullable=True)


class HcGenericRecord[S = Pending](Model[S, "HcGenericRecord[Fetched]"]):
    """Append-only raw storage for expanded Health Connect record types."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    record_type: sqlite.Col[str] = Text(nullable=False, index=True)
    record_uid: sqlite.Col[str] = Text(nullable=False, index=True)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True, index=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    payload_json: sqlite.Col[str | None] = Text(nullable=True)


class HcStepAggregateSnapshot[S = Pending](
    Model[S, "HcStepAggregateSnapshot[Fetched]"]
):
    """One accepted authoritative read of Health Connect's canonical steps."""

    snapshot_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    accepted_count: sqlite.Col[int] = Integer(nullable=False)
    deleted_count: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    installation_id: sqlite.Col[str] = Text(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, unique=True)
    skipped_count: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False, index=True)


class HcStepAggregateBucket[S = Pending](Model[S, "HcStepAggregateBucket[Fetched]"]):
    """One append-only canonical step-bucket version or tombstone."""

    version_id: sqlite.GenCol[int] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    bucket_end: sqlite.Col[int | None] = Integer(nullable=True)
    bucket_start: sqlite.Col[int] = Integer(nullable=False, index=True)
    count: sqlite.Col[int | None] = Integer(nullable=True)
    is_deleted: sqlite.Col[bool] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False, index=True)
    zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)


class HcHeartRateRecordCurrent[S = Pending](
    Model[S, "HcHeartRateRecordCurrent[Fetched]"]
):
    version_id: sqlite.Col[int] = Integer(primary_key=True)
    record_uid: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)


class HcHeartRateSampleCurrent[S = Pending](
    Model[S, "HcHeartRateSampleCurrent[Fetched]"]
):
    """A heart-rate sample whose parent version remains current."""

    sample_id: sqlite.Col[int] = Integer(primary_key=True)
    version_id: sqlite.Col[int] = Integer(nullable=False)
    sample_index: sqlite.Col[int] = Integer(nullable=False)
    time: sqlite.Col[int] = Integer(nullable=False)
    beats_per_minute: sqlite.Col[int] = Integer(nullable=False)


class HcSleepSessionCurrent[S = Pending](Model[S, "HcSleepSessionCurrent[Fetched]"]):
    version_id: sqlite.Col[int] = Integer(primary_key=True)
    record_uid: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    notes: sqlite.Col[str | None] = Text(nullable=True)


class HcSleepStageCurrent[S = Pending](Model[S, "HcSleepStageCurrent[Fetched]"]):
    """A sleep stage whose parent session version remains current."""

    stage_id: sqlite.Col[int] = Integer(primary_key=True)
    version_id: sqlite.Col[int] = Integer(nullable=False)
    stage_index: sqlite.Col[int] = Integer(nullable=False)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    stage: sqlite.Col[int] = Integer(nullable=False)


class HcStepIntervalCurrent[S = Pending](Model[S, "HcStepIntervalCurrent[Fetched]"]):
    version_id: sqlite.Col[int] = Integer(primary_key=True)
    record_uid: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    count: sqlite.Col[int | None] = Integer(nullable=True)


class HcStepAggregateBucketCurrent[S = Pending](
    Model[S, "HcStepAggregateBucketCurrent[Fetched]"]
):
    """Latest live Health Connect canonical value for one duration bucket."""

    version_id: sqlite.Col[int] = Integer(primary_key=True)
    bucket_end: sqlite.Col[int | None] = Integer(nullable=True)
    bucket_start: sqlite.Col[int] = Integer(nullable=False)
    count: sqlite.Col[int | None] = Integer(nullable=True)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    request_id: sqlite.Col[str] = Text(nullable=False)
    zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)


class HcExerciseSessionCurrent[S = Pending](
    Model[S, "HcExerciseSessionCurrent[Fetched]"]
):
    version_id: sqlite.Col[int] = Integer(primary_key=True)
    record_uid: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    exercise_type: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    notes: sqlite.Col[str | None] = Text(nullable=True)
    planned_exercise_session_id: sqlite.Col[str | None] = Text(nullable=True)


class HcGenericRecordCurrent[S = Pending](Model[S, "HcGenericRecordCurrent[Fetched]"]):
    version_id: sqlite.Col[int] = Integer(primary_key=True)
    record_type: sqlite.Col[str] = Text(nullable=False)
    record_uid: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    modified_at: sqlite.Col[int | None] = Integer(nullable=True)
    received_at: sqlite.Col[int] = Integer(nullable=False)
    client_record_id: sqlite.Col[str | None] = Text(nullable=True)
    client_record_version: sqlite.Col[int | None] = Integer(nullable=True)
    recording_method: sqlite.Col[int | None] = Integer(nullable=True)
    start_time: sqlite.Col[int | None] = Integer(nullable=True)
    end_time: sqlite.Col[int | None] = Integer(nullable=True)
    start_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    end_zone_offset_seconds: sqlite.Col[int | None] = Integer(nullable=True)
    payload_json: sqlite.Col[str | None] = Text(nullable=True)


class HcExerciseEpisodeSummary[S = Pending](
    Model[S, "HcExerciseEpisodeSummary[Fetched]"]
):
    """Deterministic aggregation of one settled exercise session version."""

    record_uid: sqlite.Col[str] = Text(primary_key=True)
    version_id: sqlite.Col[int] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    exercise_type: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    duration_minutes: sqlite.Col[float] = Real(nullable=False)
    segment_count: sqlite.Col[int] = Integer(nullable=False)
    lap_count: sqlite.Col[int] = Integer(nullable=False)
    total_lap_meters: sqlite.Col[float | None] = Real(nullable=True)
    processor_version: sqlite.Col[int] = Integer(nullable=False)


class HcSleepEpisodeSummary[S = Pending](Model[S, "HcSleepEpisodeSummary[Fetched]"]):
    """Deterministic per-stage aggregation of one settled sleep session version."""

    record_uid: sqlite.Col[str] = Text(primary_key=True)
    version_id: sqlite.Col[int] = Integer(nullable=False)
    payload_hash: sqlite.Col[str] = Text(nullable=False)
    origin_id: sqlite.Col[int | None] = Integer(nullable=True)
    title: sqlite.Col[str | None] = Text(nullable=True)
    start_time: sqlite.Col[int] = Integer(nullable=False)
    end_time: sqlite.Col[int] = Integer(nullable=False)
    duration_minutes: sqlite.Col[float] = Real(nullable=False)
    minutes_awake: sqlite.Col[float] = Real(nullable=False)
    minutes_sleeping: sqlite.Col[float] = Real(nullable=False)
    minutes_out_of_bed: sqlite.Col[float] = Real(nullable=False)
    minutes_light: sqlite.Col[float] = Real(nullable=False)
    minutes_deep: sqlite.Col[float] = Real(nullable=False)
    minutes_rem: sqlite.Col[float] = Real(nullable=False)
    minutes_awake_in_bed: sqlite.Col[float] = Real(nullable=False)
    minutes_other: sqlite.Col[float] = Real(nullable=False)
    processor_version: sqlite.Col[int] = Integer(nullable=False)


class HcEpisodeCursor[S = Pending](Model[S, "HcEpisodeCursor[Fetched]"]):
    """High-water mark of source versions already reconsidered for one type."""

    record_type: sqlite.Col[str] = Text(primary_key=True)
    last_version_id: sqlite.Col[int] = Integer(nullable=False)


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
_MODELS = [
    *_SCHEMA_MODELS,
    HcBaselineSeen,
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
    HcEpisodeCursor,
    HcStepAggregateBucket,
    HcStepAggregateSnapshot,
]
_PARENT_MODELS = {
    "exercise": HcExerciseSession,
    "heart_rate": HcHeartRateRecord,
    "sleep": HcSleepSession,
    "steps": HcStepInterval,
}


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


def health_connect_migrations() -> dict[str, str]:
    """Return the historical migration chain without changing keys or SQL bytes."""
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

    # Preserve the five keys applied immediately after the parent views by the
    # failed v3 production boot. This order lets 0.6 adopt that partial history.
    generic_start = len(legacy_statements) + 1
    for index, sql in enumerate(
        scaffold([HcGenericRecord]).splitlines(), start=generic_start
    ):
        migrations[f"{index:04d}_health_connect_schema"] = sql
    generic_view = _CURRENT_VIEW_MIGRATIONS["hc_generic_record_current"]
    migrations["0045_hc_generic_record_current"] = _create_if_not_exists(generic_view)

    for view, query in _CHILD_CURRENT_VIEW_MIGRATIONS.items():
        sql = f'CREATE VIEW "{view}" AS {query}'
        migrations[f"{next_index:04d}_{view}"] = _create_if_not_exists(sql)
        next_index += 1
    for sql in scaffold([HcBaselineSeen]).splitlines():
        migrations[f"{next_index:04d}_baseline_seen"] = _create_if_not_exists(sql)
        next_index += 1

    # Episode-summary tables: appended under fresh explicit keys so no already
    # applied migration key or statement changes.
    episode_models = [
        HcExerciseEpisodeSummary,
        HcSleepEpisodeSummary,
        HcEpisodeCursor,
    ]
    episode_start = next_index
    episode_statements = scaffold(episode_models).splitlines()
    for offset, sql in enumerate(episode_statements):
        migrations[f"{episode_start + offset:04d}_episode_summaries"] = (
            _create_if_not_exists(sql)
        )

    step_aggregate_start = episode_start + len(episode_statements)
    step_aggregate_statements = scaffold(
        [HcStepAggregateSnapshot, HcStepAggregateBucket]
    ).splitlines()
    for offset, sql in enumerate(step_aggregate_statements):
        migrations[f"{step_aggregate_start + offset:04d}_step_aggregates"] = (
            _create_if_not_exists(sql)
        )
    step_aggregate_view_index = step_aggregate_start + len(step_aggregate_statements)
    migrations[f"{step_aggregate_view_index:04d}_hc_step_aggregate_bucket_current"] = (
        _create_if_not_exists(
            'CREATE VIEW "hc_step_aggregate_bucket_current" AS SELECT parent.* FROM "hc_step_aggregate_bucket" parent WHERE parent."version_id" = (SELECT MAX(candidate."version_id") FROM "hc_step_aggregate_bucket" candidate WHERE candidate."bucket_start" = parent."bucket_start") AND parent."is_deleted" = 0'
        )
    )
    return migrations


async def create_health_connect_schema(database: Database) -> None:
    """Initialize every typed table, index, and current-version view."""
    await database.migrate(health_connect_migrations(), adopt_legacy=True)
    await database.verify(_MODELS)
