"""Bounded Health Connect current-projection aggregate queries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from snekql.sqlite import Database, Fetched, select

from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.persistence import (
    HcExerciseSessionCurrent,
    HcGenericRecordCurrent,
    HcHeartRateRecordCurrent,
    HcHeartRateSample,
    HcOrigin,
    HcSleepSessionCurrent,
    HcSleepStage,
    HcStepIntervalCurrent,
)
from tether.health_connect.telemetry_model import (
    HealthConnectDailyStepsSummary,
    HealthConnectExerciseSummary,
    HealthConnectHeartRateSummary,
    HealthConnectNumericSummary,
    HealthConnectOtherRecordSummary,
    HealthConnectSleepSummary,
    HealthConnectStepOriginSummary,
    HealthConnectStepsSummary,
    HealthConnectSummaryRead,
)
from tether.health_connect.telemetry_values import (
    datetime_from_millis,
    duration_minutes,
    latest_bound,
    local_record_date,
    millis_from_datetime,
    render_exercise_type,
    render_sleep_stage,
)

_SUMMARY_NUMERIC_SERIES_PER_TYPE = 8
"""Maximum generic measurement series returned for one summarized type."""

_SUMMARY_IGNORED_NUMERIC_FIELDS = frozenset(
    {"end_time", "start_time", "time", "zone_offset", "zone_offset_seconds"}
)
"""Generic payload fields that encode instants rather than measurements."""

_DUPLICATE_STEP_SOURCE_WARNING = (
    "Multiple step origins overlap; total_count uses the largest origin for this "
    "day and raw_total_count is the simple sum."
)
"""Tool warning for Health Connect's overlapping step sources."""


def _numeric_payload_values(
    value: object, *, path: str = ""
) -> list[tuple[str, float]]:
    """Flatten numeric measurements while unifying array indexes by path."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float):
        leaf_name = path.rsplit(".", maxsplit=1)[-1].removesuffix("[]")
        if leaf_name in _SUMMARY_IGNORED_NUMERIC_FIELDS:
            return []
        return [(path, float(value))]
    if isinstance(value, dict):
        flattened: list[tuple[str, float]] = []
        payload_fields = cast("dict[str, object]", value)
        for key, child in sorted(payload_fields.items()):
            child_path = f"{path}.{key}" if path else key
            flattened.extend(_numeric_payload_values(child, path=child_path))
        return flattened
    if isinstance(value, list):
        flattened = []
        for child in cast("list[object]", value):
            flattened.extend(_numeric_payload_values(child, path=f"{path}[]"))
        return flattened
    return []


def _step_origin_summaries(
    rows: list[HcStepIntervalCurrent[Fetched]],
    origins: dict[int, HcOrigin[Fetched]],
) -> list[HealthConnectStepOriginSummary]:
    """Group step intervals by writing app so duplicate sources are visible."""
    totals: dict[str, tuple[int, int]] = {}
    for row in rows:
        origin_package = (
            origins[row.origin_id].data_origin_package
            if row.origin_id is not None and row.origin_id in origins
            else "unknown"
        )
        record_count, total_count = totals.get(origin_package, (0, 0))
        totals[origin_package] = (record_count + 1, total_count + (row.count or 0))
    return [
        HealthConnectStepOriginSummary(
            data_origin_package=origin_package,
            record_count=record_count,
            total_count=total_count,
        )
        for origin_package, (record_count, total_count) in sorted(totals.items())
    ]


def _step_duplicate_warning(
    by_origin: list[HealthConnectStepOriginSummary],
) -> str | None:
    """Flag multi-origin step totals because Health Connect often mirrors sources."""
    if len(by_origin) <= 1:
        return None
    return _DUPLICATE_STEP_SOURCE_WARNING


def _recommended_step_total(by_origin: list[HealthConnectStepOriginSummary]) -> int:
    """Prefer one step source over summing overlapping writers."""
    return max((origin.total_count for origin in by_origin), default=0)


def _summarize_step_rows(
    rows: list[HcStepIntervalCurrent[Fetched]],
    origins: dict[int, HcOrigin[Fetched]],
    *,
    include_daily: bool,
) -> HealthConnectStepsSummary:
    """Build source-aware step totals and optional local-day buckets."""
    rows_by_date: dict[str, list[HcStepIntervalCurrent[Fetched]]] = {}
    for row in rows:
        rows_by_date.setdefault(
            local_record_date(row.start_time, row.start_zone_offset_seconds), []
        ).append(row)
    daily_summaries: list[HealthConnectDailyStepsSummary] = []
    for local_date, date_rows in sorted(rows_by_date.items()):
        by_origin = _step_origin_summaries(date_rows, origins)
        daily_summaries.append(
            HealthConnectDailyStepsSummary(
                by_origin=by_origin,
                date=local_date,
                duplicate_source_warning=_step_duplicate_warning(by_origin),
                raw_total_count=sum(row.count or 0 for row in date_rows),
                record_count=len(date_rows),
                total_count=_recommended_step_total(by_origin),
            )
        )
    by_origin = _step_origin_summaries(rows, origins)
    return HealthConnectStepsSummary(
        by_origin=by_origin,
        daily=daily_summaries if include_daily else [],
        duplicate_source_warning=_step_duplicate_warning(by_origin),
        raw_total_count=sum(row.count or 0 for row in rows),
        record_count=len(rows),
        total_count=sum(day.total_count for day in daily_summaries),
    )


@dataclass(frozen=True, slots=True)
class HealthConnectSummaryQuery:
    """Aggregate bounded current records for tool overviews and trends."""

    database: Database

    async def fetch_summary(
        self, *, after: datetime, before: datetime, bucket: Literal["none", "day"]
    ) -> HealthConnectSummaryRead:
        """Aggregate current records that overlap one bounded time window."""
        after_millis = millis_from_datetime(after)
        before_millis = millis_from_datetime(before)
        async with self.database.transaction() as transaction:
            exercise_rows = await transaction.fetch_all(
                select(HcExerciseSessionCurrent)
                .where(
                    HcExerciseSessionCurrent.end_time.gte(after_millis)
                    | (
                        HcExerciseSessionCurrent.end_time.is_null()
                        & HcExerciseSessionCurrent.start_time.gte(after_millis)
                    )
                )
                .where(HcExerciseSessionCurrent.start_time.lte(before_millis))
            )
            heart_rate_rows = await transaction.fetch_all(
                select(HcHeartRateRecordCurrent)
                .where(
                    HcHeartRateRecordCurrent.end_time.gte(after_millis)
                    | (
                        HcHeartRateRecordCurrent.end_time.is_null()
                        & HcHeartRateRecordCurrent.start_time.gte(after_millis)
                    )
                )
                .where(HcHeartRateRecordCurrent.start_time.lte(before_millis))
            )
            heart_rate_version_ids = [row.version_id for row in heart_rate_rows]
            if heart_rate_version_ids:
                heart_rate_sample_count, total_bpm = await transaction.fetch_one(
                    select(
                        HcHeartRateSample.beats_per_minute.count(),
                        HcHeartRateSample.beats_per_minute.sum(),
                    ).where(HcHeartRateSample.version_id.in_(*heart_rate_version_ids))
                )
                minimum_bpm, maximum_bpm = await transaction.fetch_one(
                    select(
                        HcHeartRateSample.beats_per_minute.min(),
                        HcHeartRateSample.beats_per_minute.max(),
                    ).where(HcHeartRateSample.version_id.in_(*heart_rate_version_ids))
                )
            else:
                heart_rate_sample_count = 0
                total_bpm = None
                minimum_bpm = None
                maximum_bpm = None
            sleep_rows = await transaction.fetch_all(
                select(HcSleepSessionCurrent)
                .where(
                    HcSleepSessionCurrent.end_time.gte(after_millis)
                    | (
                        HcSleepSessionCurrent.end_time.is_null()
                        & HcSleepSessionCurrent.start_time.gte(after_millis)
                    )
                )
                .where(HcSleepSessionCurrent.start_time.lte(before_millis))
            )
            sleep_version_ids = [row.version_id for row in sleep_rows]
            sleep_stages = (
                await transaction.fetch_all(
                    select(HcSleepStage).where(
                        HcSleepStage.version_id.in_(*sleep_version_ids)
                    )
                )
                if sleep_version_ids
                else []
            )
            step_rows = await transaction.fetch_all(
                select(HcStepIntervalCurrent)
                .where(
                    HcStepIntervalCurrent.end_time.gte(after_millis)
                    | (
                        HcStepIntervalCurrent.end_time.is_null()
                        & HcStepIntervalCurrent.start_time.gte(after_millis)
                    )
                )
                .where(HcStepIntervalCurrent.start_time.lte(before_millis))
            )
            generic_rows = await transaction.fetch_all(
                select(HcGenericRecordCurrent)
                .where(
                    HcGenericRecordCurrent.end_time.gte(after_millis)
                    | (
                        HcGenericRecordCurrent.end_time.is_null()
                        & HcGenericRecordCurrent.start_time.gte(after_millis)
                    )
                )
                .where(HcGenericRecordCurrent.start_time.lte(before_millis))
                .order_by(
                    HcGenericRecordCurrent.start_time.asc(),
                    HcGenericRecordCurrent.version_id.asc(),
                )
            )
            step_origin_ids = {
                row.origin_id for row in step_rows if row.origin_id is not None
            }
            step_origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*step_origin_ids))
                )
                if step_origin_ids
                else []
            )

        sleep_durations = [
            duration_minutes(row.start_time, row.end_time) for row in sleep_rows
        ]
        stage_code_duration_minutes: dict[str, float] = {}
        stage_duration_minutes: dict[str, float] = {}
        for stage in sleep_stages:
            stage_key = str(stage.stage)
            stage_code_duration_minutes[stage_key] = round(
                stage_code_duration_minutes.get(stage_key, 0.0)
                + duration_minutes(stage.start_time, stage.end_time),
                2,
            )
            stage_label = render_sleep_stage(stage.stage)
            stage_duration_minutes[stage_label] = round(
                stage_duration_minutes.get(stage_label, 0.0)
                + duration_minutes(stage.start_time, stage.end_time),
                2,
            )
        exercise_type_code_counts: dict[str, int] = {}
        exercise_type_counts: dict[str, int] = {}
        for row in exercise_rows:
            if row.exercise_type is None:
                continue
            exercise_type = str(row.exercise_type)
            exercise_type_code_counts[exercise_type] = (
                exercise_type_code_counts.get(exercise_type, 0) + 1
            )
            exercise_type_label = render_exercise_type(row.exercise_type)
            if exercise_type_label is not None:
                exercise_type_counts[exercise_type_label] = (
                    exercise_type_counts.get(exercise_type_label, 0) + 1
                )
        generic_by_type: dict[str, list[HcGenericRecordCurrent[Fetched]]] = {}
        for row in generic_rows:
            generic_by_type.setdefault(row.record_type, []).append(row)
        other_record_types: list[HealthConnectOtherRecordSummary] = []
        for record_type, rows in sorted(generic_by_type.items()):
            numeric_by_path: dict[str, list[float]] = {}
            for row in rows:
                for path, numeric_value in _numeric_payload_values(
                    json.loads(row.payload_json or "{}")
                ):
                    numeric_by_path.setdefault(path, []).append(numeric_value)
            numeric_values = [
                HealthConnectNumericSummary(
                    average=round(sum(values) / len(values), 4),
                    latest=values[-1],
                    maximum=max(values),
                    minimum=min(values),
                    path=path,
                    sample_count=len(values),
                )
                for path, values in sorted(numeric_by_path.items())[
                    :_SUMMARY_NUMERIC_SERIES_PER_TYPE
                ]
            ]
            other_record_types.append(
                HealthConnectOtherRecordSummary(
                    earliest_start=datetime_from_millis(
                        min(
                            row.start_time for row in rows if row.start_time is not None
                        )
                    )
                    if any(row.start_time is not None for row in rows)
                    else None,
                    latest_end=datetime_from_millis(
                        latest_bound(
                            max(
                                (
                                    row.end_time
                                    for row in rows
                                    if row.end_time is not None
                                ),
                                default=None,
                            ),
                            max(
                                (
                                    row.start_time
                                    for row in rows
                                    if row.start_time is not None
                                ),
                                default=None,
                            ),
                        )
                    ),
                    numeric_values=numeric_values,
                    record_count=len(rows),
                    record_type=cast("HealthRecordType", record_type),
                )
            )
        total_sleep_minutes = round(sum(sleep_durations), 2)
        return HealthConnectSummaryRead(
            after=after,
            before=before,
            exercise=HealthConnectExerciseSummary(
                exercise_type_code_counts=exercise_type_code_counts,
                exercise_type_counts=exercise_type_counts,
                record_count=len(exercise_rows),
                total_duration_minutes=round(
                    sum(
                        duration_minutes(row.start_time, row.end_time)
                        for row in exercise_rows
                    ),
                    2,
                ),
            ),
            heart_rate=HealthConnectHeartRateSummary(
                average_bpm=round(total_bpm / heart_rate_sample_count, 2)
                if total_bpm is not None and heart_rate_sample_count > 0
                else None,
                maximum_bpm=maximum_bpm,
                minimum_bpm=minimum_bpm,
                record_count=len(heart_rate_rows),
                sample_count=heart_rate_sample_count,
            ),
            other_record_types=other_record_types,
            sleep=HealthConnectSleepSummary(
                average_duration_minutes=round(total_sleep_minutes / len(sleep_rows), 2)
                if sleep_rows
                else None,
                record_count=len(sleep_rows),
                stage_code_duration_minutes=stage_code_duration_minutes,
                stage_duration_minutes=stage_duration_minutes,
                total_duration_minutes=total_sleep_minutes,
            ),
            steps=_summarize_step_rows(
                step_rows,
                {origin.origin_id: origin for origin in step_origins},
                include_daily=bucket == "day",
            ),
        )
