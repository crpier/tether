"""Agent-facing Health Connect inventory, summary, and record read models."""

from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel

from tether.health_connect.contracts import HealthRecordType


class HealthConnectInventoryEntry(BaseModel):
    """Current records and their observed UTC time bounds for one type."""

    earliest_start: AwareDatetime | None
    latest_end: AwareDatetime | None
    record_count: int
    record_type: HealthRecordType


class HealthConnectExerciseSummary(BaseModel):
    """Compact exercise-session measurements in a requested time window."""

    exercise_type_code_counts: dict[str, int]
    exercise_type_counts: dict[str, int]
    record_count: int
    total_duration_minutes: float


class HealthConnectHeartRateSummary(BaseModel):
    """Compact heart-rate measurements in a requested time window."""

    average_bpm: float | None
    maximum_bpm: int | None
    minimum_bpm: int | None
    record_count: int
    sample_count: int


class HealthConnectNumericSummary(BaseModel):
    """Compact descriptive values for one generic numeric payload path."""

    average: float
    latest: float
    maximum: float
    minimum: float
    path: str
    sample_count: int


class HealthConnectOtherRecordSummary(BaseModel):
    """Compact measurements for one generic Health Connect record type."""

    earliest_start: AwareDatetime | None
    latest_end: AwareDatetime | None
    numeric_values: list[HealthConnectNumericSummary]
    record_count: int
    record_type: HealthRecordType


class HealthConnectSleepSummary(BaseModel):
    """Compact sleep-session measurements in a requested time window."""

    average_duration_minutes: float | None
    record_count: int
    stage_code_duration_minutes: dict[str, float]
    stage_duration_minutes: dict[str, float]
    total_duration_minutes: float


class HealthConnectStepOriginSummary(BaseModel):
    """Step totals from one writing origin."""

    data_origin_package: str
    record_count: int
    total_count: int


class HealthConnectDailyStepsSummary(BaseModel):
    """Canonical Health Connect step total for one captured local date."""

    date: str
    total_count: int


class HealthConnectStepsSummary(BaseModel):
    """Canonical step measurements in a requested time window."""

    daily: list[HealthConnectDailyStepsSummary]
    record_count: int
    total_count: int


class HealthConnectSummaryRead(BaseModel):
    """Bounded aggregate Health Connect metrics intended for agent overviews."""

    after: AwareDatetime
    before: AwareDatetime
    exercise: HealthConnectExerciseSummary
    heart_rate: HealthConnectHeartRateSummary
    other_record_types: list[HealthConnectOtherRecordSummary]
    sleep: HealthConnectSleepSummary
    steps: HealthConnectStepsSummary


class HealthConnectOriginRead(BaseModel):
    """Writing application and device provenance returned with a record."""

    data_origin_package: str
    device_manufacturer: str | None
    device_model: str | None
    device_type: int | None


class HealthConnectRecordRead(BaseModel):
    """One latest non-tombstoned Health Connect record."""

    data: dict[str, object]
    end_time: AwareDatetime | None
    end_zone_offset_seconds: int | None
    modified_at: AwareDatetime | None
    origin: HealthConnectOriginRead | None
    received_at: AwareDatetime
    record_id: str
    record_type: HealthRecordType
    recording_method: int | None
    start_time: AwareDatetime | None
    start_zone_offset_seconds: int | None


class HealthConnectQueryRead(BaseModel):
    """Bounded current records with complete matching-set metadata."""

    records: list[dict[str, Any]]
    returned_count: int
    total_matching_count: int
    truncated: bool
