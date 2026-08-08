"""Read-only Health Connect Telemetry over current projections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any, Literal, cast

from pydantic import AwareDatetime, BaseModel
from snekql.sqlite import Database, Fetched, select

from tether.health_connect import (
    HcExerciseLap,
    HcExerciseRoutePoint,
    HcExerciseSegment,
    HcExerciseSessionCurrent,
    HcGenericRecordCurrent,
    HcHeartRateRecordCurrent,
    HcHeartRateSample,
    HcOrigin,
    HcSleepSessionCurrent,
    HcSleepStage,
    HcStepIntervalCurrent,
    HealthRecordType,
)

_SUMMARY_NUMERIC_SERIES_PER_TYPE = 8
"""Maximum generic measurement series returned for one summarized type."""

_SUMMARY_IGNORED_NUMERIC_FIELDS = frozenset(
    {"end_time", "start_time", "time", "zone_offset", "zone_offset_seconds"}
)
"""Generic payload fields that encode instants rather than measurements."""

_TOOL_NESTED_LIMIT = 50
"""Maximum nested samples/details returned by one agent tool call."""

_DUPLICATE_STEP_SOURCE_WARNING = (
    "Multiple step origins overlap; total_count uses the largest origin for this "
    "day and raw_total_count is the simple sum."
)
"""Agent-facing warning for Health Connect's overlapping step sources."""

_EXERCISE_TYPE_LABELS = {
    56: "running",
    79: "walking",
}
"""Health Connect exercise labels needed by agent summaries."""

_SLEEP_STAGE_LABELS = {
    1: "awake",
    2: "sleeping",
    3: "out_of_bed",
    4: "light",
    5: "deep",
    6: "rem",
    7: "awake_in_bed",
}
"""Health Connect sleep-stage labels needed by agent summaries."""


def _exercise_type_label(exercise_type: int | None) -> str | None:
    """Render Health Connect exercise enum values for agent-facing reads."""
    if exercise_type is None:
        return None
    return _EXERCISE_TYPE_LABELS.get(exercise_type, f"unknown_{exercise_type}")


def _sleep_stage_label(stage: int) -> str:
    """Render Health Connect sleep-stage enum values for agent-facing reads."""
    return _SLEEP_STAGE_LABELS.get(stage, f"unknown_{stage}")


def _local_record_date(start_time: int | None, zone_offset_seconds: int | None) -> str:
    """Bucket records by their captured local date when Health Connect provides it."""
    if start_time is None:
        return "unknown"
    return (
        (
            datetime.fromtimestamp(start_time / 1000, UTC)
            + timedelta(seconds=zone_offset_seconds or 0)
        )
        .date()
        .isoformat()
    )


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
    """Step totals for one captured local date."""

    by_origin: list[HealthConnectStepOriginSummary]
    date: str
    duplicate_source_warning: str | None
    raw_total_count: int
    record_count: int
    total_count: int


class HealthConnectStepsSummary(BaseModel):
    """Compact step measurements in a requested time window."""

    by_origin: list[HealthConnectStepOriginSummary]
    daily: list[HealthConnectDailyStepsSummary]
    duplicate_source_warning: str | None
    raw_total_count: int
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


_HEALTH_RECORD_DATA_LIMIT_BYTES = 4 * 1_024
"""Maximum raw data retained for one queried Health Connect record."""


def _bounded_record_result(record: HealthConnectRecordRead) -> dict[str, Any]:
    """Keep raw reflected data from injecting unbounded agent context."""
    record_result = record.model_dump(mode="json")
    record_data = cast("dict[str, object]", record_result["data"])
    data_size_bytes = len(
        dumps(record_data, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if data_size_bytes > _HEALTH_RECORD_DATA_LIMIT_BYTES:
        record_result["data"] = {
            "original_size_bytes": data_size_bytes,
            "truncated": True,
        }
    return record_result


def _datetime_from_millis(value: int | None) -> datetime | None:
    """Render Health Connect epoch milliseconds as an unambiguous UTC instant."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000, UTC)


def _millis_from_datetime(value: datetime) -> int:
    """Convert a validated aware tool-boundary instant to epoch milliseconds."""
    return int(value.timestamp() * 1_000)


def _duration_minutes(start_time: int | None, end_time: int | None) -> float:
    """Return a non-negative interval duration in minutes."""
    if start_time is None or end_time is None:
        return 0.0
    return max(0.0, (end_time - start_time) / 60_000)


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


def _latest_bound(latest_end: int | None, latest_start: int | None) -> int | None:
    """Use the latest available endpoint while retaining instant-only records."""
    bounds = [value for value in (latest_end, latest_start) if value is not None]
    return max(bounds) if bounds else None


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
            _local_record_date(row.start_time, row.start_zone_offset_seconds), []
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
class HealthConnectTelemetry:
    """Current-projection reads that never expose append-only history.

    Example:
        telemetry = HealthConnectTelemetry(database)
        inventory = await telemetry.fetch_inventory()
    """

    database: Database

    async def fetch_inventory(self) -> list[HealthConnectInventoryEntry]:
        """List populated current projections with their observed time bounds."""
        async with self.database.transaction() as transaction:
            exercise_stats = await transaction.fetch_one(
                select(
                    HcExerciseSessionCurrent.version_id.count(),
                    HcExerciseSessionCurrent.start_time.min(),
                    HcExerciseSessionCurrent.end_time.max(),
                ).all()
            )
            exercise_latest_start = await transaction.fetch_one(
                select(HcExerciseSessionCurrent.start_time.max()).all()
            )
            heart_rate_stats = await transaction.fetch_one(
                select(
                    HcHeartRateRecordCurrent.version_id.count(),
                    HcHeartRateRecordCurrent.start_time.min(),
                    HcHeartRateRecordCurrent.end_time.max(),
                ).all()
            )
            heart_rate_latest_start = await transaction.fetch_one(
                select(HcHeartRateRecordCurrent.start_time.max()).all()
            )
            sleep_stats = await transaction.fetch_one(
                select(
                    HcSleepSessionCurrent.version_id.count(),
                    HcSleepSessionCurrent.start_time.min(),
                    HcSleepSessionCurrent.end_time.max(),
                ).all()
            )
            sleep_latest_start = await transaction.fetch_one(
                select(HcSleepSessionCurrent.start_time.max()).all()
            )
            steps_stats = await transaction.fetch_one(
                select(
                    HcStepIntervalCurrent.version_id.count(),
                    HcStepIntervalCurrent.start_time.min(),
                    HcStepIntervalCurrent.end_time.max(),
                ).all()
            )
            steps_latest_start = await transaction.fetch_one(
                select(HcStepIntervalCurrent.start_time.max()).all()
            )
            generic_rows = await transaction.fetch_all(
                select(
                    HcGenericRecordCurrent.record_type,
                    HcGenericRecordCurrent.version_id.count(),
                    HcGenericRecordCurrent.start_time.min(),
                )
                .all()
                .group_by(HcGenericRecordCurrent.record_type)
            )
            generic_latest_ends = dict(
                await transaction.fetch_all(
                    select(
                        HcGenericRecordCurrent.record_type,
                        HcGenericRecordCurrent.end_time.max(),
                    )
                    .all()
                    .group_by(HcGenericRecordCurrent.record_type)
                )
            )
            generic_latest_starts = dict(
                await transaction.fetch_all(
                    select(
                        HcGenericRecordCurrent.record_type,
                        HcGenericRecordCurrent.start_time.max(),
                    )
                    .all()
                    .group_by(HcGenericRecordCurrent.record_type)
                )
            )
        typed_rows: list[
            tuple[HealthRecordType, tuple[int, int | None, int | None, int | None]]
        ] = [
            ("exercise", (*exercise_stats, exercise_latest_start)),
            ("heart_rate", (*heart_rate_stats, heart_rate_latest_start)),
            ("sleep", (*sleep_stats, sleep_latest_start)),
            ("steps", (*steps_stats, steps_latest_start)),
        ]
        entries: list[HealthConnectInventoryEntry] = []
        for record_type, (
            count,
            earliest_start,
            latest_end,
            latest_start,
        ) in typed_rows:
            if count == 0:
                continue
            entries.append(
                HealthConnectInventoryEntry(
                    earliest_start=_datetime_from_millis(earliest_start),
                    latest_end=_datetime_from_millis(
                        _latest_bound(latest_end, latest_start)
                    ),
                    record_count=count,
                    record_type=record_type,
                )
            )
        for record_type, count, earliest_start in generic_rows:
            entries.append(
                HealthConnectInventoryEntry(
                    earliest_start=_datetime_from_millis(earliest_start),
                    latest_end=_datetime_from_millis(
                        _latest_bound(
                            generic_latest_ends[record_type],
                            generic_latest_starts[record_type],
                        )
                    ),
                    record_count=count,
                    record_type=cast("HealthRecordType", record_type),
                )
            )
        return sorted(entries, key=lambda entry: entry.record_type)

    async def fetch_summary(
        self, *, after: datetime, before: datetime, bucket: Literal["none", "day"]
    ) -> HealthConnectSummaryRead:
        """Aggregate current records that overlap one bounded time window."""
        after_millis = _millis_from_datetime(after)
        before_millis = _millis_from_datetime(before)
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
            _duration_minutes(row.start_time, row.end_time) for row in sleep_rows
        ]
        stage_code_duration_minutes: dict[str, float] = {}
        stage_duration_minutes: dict[str, float] = {}
        for stage in sleep_stages:
            stage_key = str(stage.stage)
            stage_code_duration_minutes[stage_key] = round(
                stage_code_duration_minutes.get(stage_key, 0.0)
                + _duration_minutes(stage.start_time, stage.end_time),
                2,
            )
            stage_label = _sleep_stage_label(stage.stage)
            stage_duration_minutes[stage_label] = round(
                stage_duration_minutes.get(stage_label, 0.0)
                + _duration_minutes(stage.start_time, stage.end_time),
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
            exercise_type_label = _exercise_type_label(row.exercise_type)
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
                    earliest_start=_datetime_from_millis(
                        min(
                            row.start_time for row in rows if row.start_time is not None
                        )
                    )
                    if any(row.start_time is not None for row in rows)
                    else None,
                    latest_end=_datetime_from_millis(
                        _latest_bound(
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
                        _duration_minutes(row.start_time, row.end_time)
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

    async def fetch_records(
        self,
        *,
        record_type: HealthRecordType,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> HealthConnectQueryRead:
        """Read one bounded current projection with matching-set metadata."""
        if record_type == "exercise":
            records = await self._fetch_exercise(
                after=after, before=before, limit=limit
            )
            total_matching_count = await self._count_current_exercises(
                after=after, before=before
            )
        elif record_type == "heart_rate":
            records = await self._fetch_heart_rates(
                after=after, before=before, limit=limit
            )
            total_matching_count = await self._count_current_heart_rates(
                after=after, before=before
            )
        elif record_type == "sleep":
            records = await self._fetch_sleep(after=after, before=before, limit=limit)
            total_matching_count = await self._count_current_sleep(
                after=after, before=before
            )
        elif record_type == "steps":
            records = await self._fetch_steps(after=after, before=before, limit=limit)
            total_matching_count = await self._count_current_steps(
                after=after, before=before
            )
        else:
            records = await self._fetch_generic(
                record_type=record_type,
                after=after,
                before=before,
                limit=limit,
            )
            total_matching_count = await self._count_current_generic(
                record_type=record_type,
                after=after,
                before=before,
            )
        return HealthConnectQueryRead(
            records=[_bounded_record_result(record) for record in records],
            returned_count=len(records),
            total_matching_count=total_matching_count,
            truncated=total_matching_count > len(records),
        )

    async def _count_current_exercises(
        self, *, after: datetime | None, before: datetime | None
    ) -> int:
        """Count exercise records matching raw-read bounds."""
        query = select(HcExerciseSessionCurrent.version_id.count())
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcExerciseSessionCurrent.end_time.gte(after_millis)
                | (
                    HcExerciseSessionCurrent.end_time.is_null()
                    & HcExerciseSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcExerciseSessionCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _count_current_generic(
        self,
        *,
        record_type: HealthRecordType,
        after: datetime | None,
        before: datetime | None,
    ) -> int:
        """Count expanded generic records matching raw-read bounds."""
        query = select(HcGenericRecordCurrent.version_id.count()).where(
            HcGenericRecordCurrent.record_type.eq(record_type)
        )
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcGenericRecordCurrent.end_time.gte(after_millis)
                | (
                    HcGenericRecordCurrent.end_time.is_null()
                    & HcGenericRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcGenericRecordCurrent.start_time.lte(_millis_from_datetime(before))
            )
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _count_current_heart_rates(
        self, *, after: datetime | None, before: datetime | None
    ) -> int:
        """Count heart-rate records matching raw-read bounds."""
        query = select(HcHeartRateRecordCurrent.version_id.count())
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcHeartRateRecordCurrent.end_time.gte(after_millis)
                | (
                    HcHeartRateRecordCurrent.end_time.is_null()
                    & HcHeartRateRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcHeartRateRecordCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _count_current_sleep(
        self, *, after: datetime | None, before: datetime | None
    ) -> int:
        """Count sleep records matching raw-read bounds."""
        query = select(HcSleepSessionCurrent.version_id.count())
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcSleepSessionCurrent.end_time.gte(after_millis)
                | (
                    HcSleepSessionCurrent.end_time.is_null()
                    & HcSleepSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcSleepSessionCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _count_current_steps(
        self, *, after: datetime | None, before: datetime | None
    ) -> int:
        """Count step records matching raw-read bounds."""
        query = select(HcStepIntervalCurrent.version_id.count())
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcStepIntervalCurrent.end_time.gte(after_millis)
                | (
                    HcStepIntervalCurrent.end_time.is_null()
                    & HcStepIntervalCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcStepIntervalCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _fetch_steps(
        self,
        *,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> list[HealthConnectRecordRead]:
        """Read bounded current step intervals overlapping an aware time window."""
        query = select(HcStepIntervalCurrent)
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcStepIntervalCurrent.end_time.gte(after_millis)
                | (
                    HcStepIntervalCurrent.end_time.is_null()
                    & HcStepIntervalCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcStepIntervalCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        query = query.order_by(
            HcStepIntervalCurrent.start_time.desc(),
            HcStepIntervalCurrent.version_id.desc(),
        ).limit(limit)
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(query)
            origin_ids = {row.origin_id for row in rows if row.origin_id is not None}
            origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*origin_ids))
                )
                if origin_ids
                else []
            )
        origins_by_id = {origin.origin_id: origin for origin in origins}
        return [
            HealthConnectRecordRead(
                data={"count": row.count},
                end_time=_datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=_datetime_from_millis(row.modified_at),
                origin=(
                    None
                    if row.origin_id is None
                    else HealthConnectOriginRead(
                        data_origin_package=origins_by_id[
                            row.origin_id
                        ].data_origin_package,
                        device_manufacturer=origins_by_id[
                            row.origin_id
                        ].device_manufacturer,
                        device_model=origins_by_id[row.origin_id].device_model,
                        device_type=origins_by_id[row.origin_id].device_type,
                    )
                ),
                received_at=cast("datetime", _datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="steps",
                recording_method=row.recording_method,
                start_time=_datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]

    async def _fetch_heart_rates(
        self,
        *,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> list[HealthConnectRecordRead]:
        """Read bounded current heart-rate records and ordered samples."""
        query = select(HcHeartRateRecordCurrent)
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcHeartRateRecordCurrent.end_time.gte(after_millis)
                | (
                    HcHeartRateRecordCurrent.end_time.is_null()
                    & HcHeartRateRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcHeartRateRecordCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        query = query.order_by(
            HcHeartRateRecordCurrent.start_time.desc(),
            HcHeartRateRecordCurrent.version_id.desc(),
        ).limit(limit)
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(query)
            version_ids = [row.version_id for row in rows]
            samples = (
                await transaction.fetch_all(
                    select(HcHeartRateSample)
                    .where(HcHeartRateSample.version_id.in_(*version_ids))
                    .order_by(
                        HcHeartRateSample.version_id.desc(),
                        HcHeartRateSample.sample_index.asc(),
                    )
                    .limit(_TOOL_NESTED_LIMIT + 1)
                )
                if version_ids
                else []
            )
            origin_ids = {row.origin_id for row in rows if row.origin_id is not None}
            origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*origin_ids))
                )
                if origin_ids
                else []
            )
        origins_by_id = {origin.origin_id: origin for origin in origins}
        return [
            HealthConnectRecordRead(
                data={
                    "samples": [
                        {
                            "beats_per_minute": sample.beats_per_minute,
                            "time": _datetime_from_millis(sample.time),
                        }
                        for sample in samples[:_TOOL_NESTED_LIMIT]
                        if sample.version_id == row.version_id
                    ],
                    "samples_truncated": len(samples) > _TOOL_NESTED_LIMIT,
                },
                end_time=_datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=_datetime_from_millis(row.modified_at),
                origin=(
                    None
                    if row.origin_id is None
                    else HealthConnectOriginRead(
                        data_origin_package=origins_by_id[
                            row.origin_id
                        ].data_origin_package,
                        device_manufacturer=origins_by_id[
                            row.origin_id
                        ].device_manufacturer,
                        device_model=origins_by_id[row.origin_id].device_model,
                        device_type=origins_by_id[row.origin_id].device_type,
                    )
                ),
                received_at=cast("datetime", _datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="heart_rate",
                recording_method=row.recording_method,
                start_time=_datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]

    async def _fetch_sleep(
        self,
        *,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> list[HealthConnectRecordRead]:
        """Read bounded current sleep sessions and ordered stages."""
        query = select(HcSleepSessionCurrent)
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcSleepSessionCurrent.end_time.gte(after_millis)
                | (
                    HcSleepSessionCurrent.end_time.is_null()
                    & HcSleepSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcSleepSessionCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        query = query.order_by(
            HcSleepSessionCurrent.start_time.desc(),
            HcSleepSessionCurrent.version_id.desc(),
        ).limit(limit)
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(query)
            version_ids = [row.version_id for row in rows]
            stages = (
                await transaction.fetch_all(
                    select(HcSleepStage)
                    .where(HcSleepStage.version_id.in_(*version_ids))
                    .order_by(
                        HcSleepStage.version_id.desc(),
                        HcSleepStage.stage_index.asc(),
                    )
                    .limit(_TOOL_NESTED_LIMIT + 1)
                )
                if version_ids
                else []
            )
            origin_ids = {row.origin_id for row in rows if row.origin_id is not None}
            origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*origin_ids))
                )
                if origin_ids
                else []
            )
        origins_by_id = {origin.origin_id: origin for origin in origins}
        return [
            HealthConnectRecordRead(
                data={
                    "notes": row.notes,
                    "stages": [
                        {
                            "end_time": _datetime_from_millis(stage.end_time),
                            "stage": stage.stage,
                            "stage_label": _sleep_stage_label(stage.stage),
                            "start_time": _datetime_from_millis(stage.start_time),
                        }
                        for stage in stages[:_TOOL_NESTED_LIMIT]
                        if stage.version_id == row.version_id
                    ],
                    "stages_truncated": len(stages) > _TOOL_NESTED_LIMIT,
                    "title": row.title,
                },
                end_time=_datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=_datetime_from_millis(row.modified_at),
                origin=(
                    None
                    if row.origin_id is None
                    else HealthConnectOriginRead(
                        data_origin_package=origins_by_id[
                            row.origin_id
                        ].data_origin_package,
                        device_manufacturer=origins_by_id[
                            row.origin_id
                        ].device_manufacturer,
                        device_model=origins_by_id[row.origin_id].device_model,
                        device_type=origins_by_id[row.origin_id].device_type,
                    )
                ),
                received_at=cast("datetime", _datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="sleep",
                recording_method=row.recording_method,
                start_time=_datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]

    async def _fetch_exercise(
        self,
        *,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> list[HealthConnectRecordRead]:
        """Read bounded current exercise sessions and nested details."""
        query = select(HcExerciseSessionCurrent)
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcExerciseSessionCurrent.end_time.gte(after_millis)
                | (
                    HcExerciseSessionCurrent.end_time.is_null()
                    & HcExerciseSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcExerciseSessionCurrent.start_time.lte(_millis_from_datetime(before))
            )
        if after is None and before is None:
            query = query.all()
        query = query.order_by(
            HcExerciseSessionCurrent.start_time.desc(),
            HcExerciseSessionCurrent.version_id.desc(),
        ).limit(limit)
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(query)
            version_ids = [row.version_id for row in rows]
            segments = (
                await transaction.fetch_all(
                    select(HcExerciseSegment)
                    .where(HcExerciseSegment.version_id.in_(*version_ids))
                    .order_by(
                        HcExerciseSegment.version_id.desc(),
                        HcExerciseSegment.segment_index.asc(),
                    )
                    .limit(_TOOL_NESTED_LIMIT + 1)
                )
                if version_ids
                else []
            )
            laps = (
                await transaction.fetch_all(
                    select(HcExerciseLap)
                    .where(HcExerciseLap.version_id.in_(*version_ids))
                    .order_by(
                        HcExerciseLap.version_id.desc(),
                        HcExerciseLap.lap_index.asc(),
                    )
                    .limit(_TOOL_NESTED_LIMIT + 1)
                )
                if version_ids
                else []
            )
            route = (
                await transaction.fetch_all(
                    select(HcExerciseRoutePoint)
                    .where(HcExerciseRoutePoint.version_id.in_(*version_ids))
                    .order_by(
                        HcExerciseRoutePoint.version_id.desc(),
                        HcExerciseRoutePoint.point_index.asc(),
                    )
                    .limit(_TOOL_NESTED_LIMIT + 1)
                )
                if version_ids
                else []
            )
            origin_ids = {row.origin_id for row in rows if row.origin_id is not None}
            origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*origin_ids))
                )
                if origin_ids
                else []
            )
        origins_by_id = {origin.origin_id: origin for origin in origins}
        return [
            HealthConnectRecordRead(
                data={
                    "exercise_type": row.exercise_type,
                    "exercise_type_label": _exercise_type_label(row.exercise_type),
                    "laps": [
                        {
                            "end_time": _datetime_from_millis(lap.end_time),
                            "length_meters": lap.length_meters,
                            "start_time": _datetime_from_millis(lap.start_time),
                        }
                        for lap in laps[:_TOOL_NESTED_LIMIT]
                        if lap.version_id == row.version_id
                    ],
                    "nested_truncated": {
                        "laps": len(laps) > _TOOL_NESTED_LIMIT,
                        "route": len(route) > _TOOL_NESTED_LIMIT,
                        "segments": len(segments) > _TOOL_NESTED_LIMIT,
                    },
                    "notes": row.notes,
                    "planned_exercise_session_id": row.planned_exercise_session_id,
                    "route": [
                        {
                            "altitude_meters": point.altitude_meters,
                            "horizontal_accuracy_meters": (
                                point.horizontal_accuracy_meters
                            ),
                            "latitude": point.latitude,
                            "longitude": point.longitude,
                            "time": _datetime_from_millis(point.time),
                            "vertical_accuracy_meters": (
                                point.vertical_accuracy_meters
                            ),
                        }
                        for point in route[:_TOOL_NESTED_LIMIT]
                        if point.version_id == row.version_id
                    ],
                    "segments": [
                        {
                            "end_time": _datetime_from_millis(segment.end_time),
                            "repetitions_count": segment.repetitions_count,
                            "segment_type": segment.segment_type,
                            "start_time": _datetime_from_millis(segment.start_time),
                        }
                        for segment in segments[:_TOOL_NESTED_LIMIT]
                        if segment.version_id == row.version_id
                    ],
                    "title": row.title,
                },
                end_time=_datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=_datetime_from_millis(row.modified_at),
                origin=(
                    None
                    if row.origin_id is None
                    else HealthConnectOriginRead(
                        data_origin_package=origins_by_id[
                            row.origin_id
                        ].data_origin_package,
                        device_manufacturer=origins_by_id[
                            row.origin_id
                        ].device_manufacturer,
                        device_model=origins_by_id[row.origin_id].device_model,
                        device_type=origins_by_id[row.origin_id].device_type,
                    )
                ),
                received_at=cast("datetime", _datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="exercise",
                recording_method=row.recording_method,
                start_time=_datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]

    async def _fetch_generic(
        self,
        *,
        record_type: HealthRecordType,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> list[HealthConnectRecordRead]:
        """Read bounded current records from one expanded v3 projection."""
        query = select(HcGenericRecordCurrent).where(
            HcGenericRecordCurrent.record_type.eq(record_type)
        )
        if after is not None:
            after_millis = _millis_from_datetime(after)
            query = query.where(
                HcGenericRecordCurrent.end_time.gte(after_millis)
                | (
                    HcGenericRecordCurrent.end_time.is_null()
                    & HcGenericRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcGenericRecordCurrent.start_time.lte(_millis_from_datetime(before))
            )
        query = query.order_by(
            HcGenericRecordCurrent.start_time.desc(),
            HcGenericRecordCurrent.version_id.desc(),
        ).limit(limit)
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(query)
            origin_ids = {row.origin_id for row in rows if row.origin_id is not None}
            origins = (
                await transaction.fetch_all(
                    select(HcOrigin).where(HcOrigin.origin_id.in_(*origin_ids))
                )
                if origin_ids
                else []
            )
        origins_by_id = {origin.origin_id: origin for origin in origins}
        return [
            HealthConnectRecordRead(
                data=json.loads(row.payload_json or "{}"),
                end_time=_datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=_datetime_from_millis(row.modified_at),
                origin=(
                    None
                    if row.origin_id is None
                    else HealthConnectOriginRead(
                        data_origin_package=origins_by_id[
                            row.origin_id
                        ].data_origin_package,
                        device_manufacturer=origins_by_id[
                            row.origin_id
                        ].device_manufacturer,
                        device_model=origins_by_id[row.origin_id].device_model,
                        device_type=origins_by_id[row.origin_id].device_type,
                    )
                ),
                received_at=cast("datetime", _datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type=record_type,
                recording_method=row.recording_method,
                start_time=_datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]
