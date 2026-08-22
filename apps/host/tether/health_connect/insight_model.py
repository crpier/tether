"""Compact deterministic Health Connect insight read models."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel

from tether.health_connect.contracts import HealthRecordType


class HealthConnectStageHeartRateRead(BaseModel):
    """Observed heart-rate samples aligned to one sleep stage label."""

    average_bpm: float
    maximum_bpm: int
    minimum_bpm: int
    sample_count: int


class HealthConnectSleepingHeartRateRead(BaseModel):
    """Observed heart-rate samples aligned to one sleep episode."""

    average_bpm: float
    by_stage: dict[str, HealthConnectStageHeartRateRead]
    maximum_bpm: int
    minimum_bpm: int
    sample_count: int


class HealthConnectSleepEpisodeRead(BaseModel):
    """Measured structure and provenance for one sleep episode."""

    classification: Literal["nap", "other", "primary_sleep"]
    evidence_uri: str
    local_end: AwareDatetime
    local_start: AwareDatetime
    record_id: str
    sleep_efficiency_percent: float | None
    sleeping_heart_rate: HealthConnectSleepingHeartRateRead | None
    source_version: int
    stage_coverage_percent: float
    stage_interval_count: int
    stage_minutes: dict[str, float]
    stage_percent_of_time_asleep: dict[str, float]
    stages_complete: bool
    time_asleep_minutes: float
    time_in_bed_minutes: float


class HealthConnectSleepBaselineDeltaRead(BaseModel):
    """Selected episode differences from its same-kind personal baseline."""

    sleep_efficiency_percentage_points: float
    stage_percentage_points: dict[str, float]
    time_asleep_minutes: float
    time_in_bed_minutes: float


class HealthConnectSleepBaselineRead(BaseModel):
    """Median metrics from comparable prior sleep episodes."""

    classification: Literal["nap", "other", "primary_sleep"]
    comparison_episode_count: int
    median_sleep_efficiency_percent: float | None
    median_stage_percent_of_time_asleep: dict[str, float]
    median_time_asleep_minutes: float | None
    median_time_in_bed_minutes: float | None
    period_days: int
    selected_delta: HealthConnectSleepBaselineDeltaRead | None


class HealthConnectSleepDayRead(BaseModel):
    """Combined measured sleep ending on one captured local date."""

    date: str
    episode_count: int
    evidence_uris: list[str]
    nap_count: int
    primary_sleep_count: int
    stage_minutes: dict[str, float]
    time_asleep_minutes: float
    time_in_bed_minutes: float


class HealthConnectDailySleepTrendRead(BaseModel):
    """One local sleep day used in a bounded trend."""

    date: str
    evidence_uris: list[str]
    nap_count: int
    primary_sleep_count: int
    primary_sleep_efficiency_percent: float | None
    primary_sleep_evidence_uri: str | None
    primary_sleep_local_end: AwareDatetime | None
    primary_sleep_local_start: AwareDatetime | None
    primary_sleeping_heart_rate_bpm: float | None
    primary_sleeping_heart_rate_sample_count: int | None
    stage_minutes: dict[str, float]
    time_asleep_minutes: float
    time_in_bed_minutes: float


class HealthConnectSleepTrendWindowRead(BaseModel):
    """Comparable primary-sleep averages over one seven-day window."""

    average_sleep_efficiency_percent: float | None
    average_sleeping_heart_rate_bpm: float | None
    average_stage_percent_of_time_asleep: dict[str, float]
    average_time_asleep_minutes: float | None
    end_date: str
    primary_sleep_count: int
    start_date: str


class HealthConnectSleepTrendCoverageRead(BaseModel):
    """Episode, sleep-day, and stage availability in the trend period."""

    nap_count: int
    primary_sleep_count: int
    sleep_day_count: int
    stage_complete_episode_count: int
    total_episode_count: int


class HealthConnectSleepTrendComparisonRead(BaseModel):
    """The latest seven local dates beside the preceding seven."""

    current_7_days: HealthConnectSleepTrendWindowRead
    previous_7_days: HealthConnectSleepTrendWindowRead


class HealthConnectSleepTrendInsightRead(BaseModel):
    """Bounded daily sleep observations and comparable recent windows."""

    comparison: HealthConnectSleepTrendComparisonRead | None
    coverage: HealthConnectSleepTrendCoverageRead
    daily: list[HealthConnectDailySleepTrendRead]
    daily_truncated: bool
    focus: Literal["sleep_trend"] = "sleep_trend"
    interpretation_limits: str = (
        "Deterministic personal observations only; not a medical diagnosis."
    )
    requested_days: int
    status: Literal["available", "no_sleep_data"]


class HealthConnectSleepingHeartRateObservationRead(BaseModel):
    """One primary sleep's heart-rate observation relative to baseline."""

    average_bpm: float
    date: str
    difference_from_baseline_bpm: float | None
    evidence_uri: str
    sample_count: int


class HealthConnectSleepingHeartRateBaselineRead(BaseModel):
    """Prior primary sleeps used as the personal heart-rate baseline."""

    comparison_episode_count: int
    latest_difference_bpm: float | None
    median_bpm: float | None


class HealthConnectSleepingHeartRateCoverageRead(BaseModel):
    """Primary-sleep and sleeping-heart-rate availability counts."""

    primary_sleep_count: int
    with_heart_rate_count: int


class HealthConnectSleepingHeartRateWindowComparisonRead(BaseModel):
    """Recent sleeping heart rate beside the preceding seven days."""

    current_7_days_average_bpm: float | None
    current_7_days_episode_count: int
    difference_bpm: float | None
    previous_7_days_average_bpm: float | None
    previous_7_days_episode_count: int


class HealthConnectSleepingHeartRateInsightRead(BaseModel):
    """Sleep-aligned heart rate with personal baseline and coverage."""

    baseline: HealthConnectSleepingHeartRateBaselineRead
    coverage: HealthConnectSleepingHeartRateCoverageRead
    focus: Literal["sleeping_heart_rate"] = "sleeping_heart_rate"
    interpretation_limits: str = (
        "Deterministic personal observations only; not a medical diagnosis."
    )
    observations: list[HealthConnectSleepingHeartRateObservationRead]
    requested_days: int
    status: Literal["available", "no_sleeping_heart_rate"]
    window_comparison: HealthConnectSleepingHeartRateWindowComparisonRead


class HealthConnectMetricStatusRead(BaseModel):
    """Availability and synchronization state for one supported metric."""

    explanation: str
    focus: Literal["metric_status"] = "metric_status"
    record_count: int
    record_type: HealthRecordType
    status: Literal["available", "not_synchronized", "synchronized_no_records"]
    sync_configured: bool
    supported: Literal[True] = True


class HealthConnectSleepEpisodeInsightRead(BaseModel):
    """One requested sleep episode with enough context for a chat answer."""

    baseline: HealthConnectSleepBaselineRead | None = None
    focus: Literal["sleep_episode"] = "sleep_episode"
    interpretation_limits: str = (
        "Deterministic personal observations only; not a medical diagnosis."
    )
    requested_days: int
    selected_episode: HealthConnectSleepEpisodeRead | None
    sleep_day: HealthConnectSleepDayRead | None = None
    status: Literal["available", "no_matching_episode"]
