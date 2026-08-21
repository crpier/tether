"""Canonical Health Connect SQLite models and byte-stable schema chain."""

from __future__ import annotations

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


class HcExerciseEpisodeSummary[S = Pending](
    Model[S, "HcExerciseEpisodeSummary[Fetched]"]
):
    """Deterministic aggregation of one settled exercise session version."""

    record_uid: HcExerciseEpisodeSummary.Col[str] = Text(primary_key=True)
    version_id: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)
    payload_hash: HcExerciseEpisodeSummary.Col[str] = Text(nullable=False)
    origin_id: HcExerciseEpisodeSummary.Col[int | None] = Integer(nullable=True)
    exercise_type: HcExerciseEpisodeSummary.Col[int | None] = Integer(nullable=True)
    title: HcExerciseEpisodeSummary.Col[str | None] = Text(nullable=True)
    start_time: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)
    end_time: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)
    duration_minutes: HcExerciseEpisodeSummary.Col[float] = Real(nullable=False)
    segment_count: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)
    lap_count: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)
    total_lap_meters: HcExerciseEpisodeSummary.Col[float | None] = Real(nullable=True)
    processor_version: HcExerciseEpisodeSummary.Col[int] = Integer(nullable=False)


class HcSleepEpisodeSummary[S = Pending](Model[S, "HcSleepEpisodeSummary[Fetched]"]):
    """Deterministic per-stage aggregation of one settled sleep session version."""

    record_uid: HcSleepEpisodeSummary.Col[str] = Text(primary_key=True)
    version_id: HcSleepEpisodeSummary.Col[int] = Integer(nullable=False)
    payload_hash: HcSleepEpisodeSummary.Col[str] = Text(nullable=False)
    origin_id: HcSleepEpisodeSummary.Col[int | None] = Integer(nullable=True)
    title: HcSleepEpisodeSummary.Col[str | None] = Text(nullable=True)
    start_time: HcSleepEpisodeSummary.Col[int] = Integer(nullable=False)
    end_time: HcSleepEpisodeSummary.Col[int] = Integer(nullable=False)
    duration_minutes: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_awake: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_sleeping: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_out_of_bed: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_light: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_deep: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_rem: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_awake_in_bed: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    minutes_other: HcSleepEpisodeSummary.Col[float] = Real(nullable=False)
    processor_version: HcSleepEpisodeSummary.Col[int] = Integer(nullable=False)


class HcEpisodeCursor[S = Pending](Model[S, "HcEpisodeCursor[Fetched]"]):
    """High-water mark of source versions already reconsidered for one type."""

    record_type: HcEpisodeCursor.Col[str] = Text(primary_key=True)
    last_version_id: HcEpisodeCursor.Col[int] = Integer(nullable=False)


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

    # Episode-summary tables: appended under fresh explicit keys so no already
    # applied migration key or statement changes.
    episode_models = [
        HcExerciseEpisodeSummary,
        HcSleepEpisodeSummary,
        HcEpisodeCursor,
    ]
    episode_start = next_index
    for offset, sql in enumerate(scaffold(episode_models).splitlines()):
        migrations[f"{episode_start + offset:04d}_episode_summaries"] = (
            _create_if_not_exists(sql)
        )
    return migrations


async def create_health_connect_schema(database: Database) -> None:
    """Initialize every typed table, index, and current-version view."""
    await database.migrate(health_connect_migrations())
    await database.verify(_MODELS)
