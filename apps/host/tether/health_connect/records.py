"""Bounded Health Connect current-record queries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from json import dumps
from typing import Any, cast

from snekql.sqlite import Database, Fetched, select

from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.persistence import (
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
)
from tether.health_connect.telemetry_model import (
    HealthConnectOriginRead,
    HealthConnectQueryRead,
    HealthConnectRecordRead,
)
from tether.health_connect.telemetry_values import (
    datetime_from_millis,
    duration_minutes,
    millis_from_datetime,
    render_exercise_type,
    render_sleep_stage,
    stage_coverage_is_complete,
)

_TOOL_NESTED_LIMIT = 50
"""Maximum nested samples/details returned by one agent tool call."""

_HEALTH_RECORD_DATA_LIMIT_BYTES = 4 * 1_024
"""Maximum reflected data retained for one queried Health Connect record."""

_TYPED_NESTED_DATA_LIMIT_BYTES = 32 * 1_024
"""Larger cap for schema-controlled exercise and heart-rate detail."""


def _bounded_record_result(record: HealthConnectRecordRead) -> dict[str, Any]:
    """Keep raw reflected data from injecting unbounded agent context."""
    record_result = record.model_dump(mode="json")
    record_data = cast("dict[str, object]", record_result["data"])
    data_size_bytes = len(
        dumps(record_data, ensure_ascii=False, separators=(",", ":")).encode()
    )
    data_limit_bytes = (
        _TYPED_NESTED_DATA_LIMIT_BYTES
        if record.record_type in {"exercise", "heart_rate"}
        else _HEALTH_RECORD_DATA_LIMIT_BYTES
    )
    if data_size_bytes > data_limit_bytes:
        if "summary" in record_data:
            record_result["data"] = {
                "evidence_uri": record_data.get("evidence_uri"),
                "original_size_bytes": data_size_bytes,
                "source_version": record_data.get("source_version"),
                "summary": record_data["summary"],
                "timeline_omitted": True,
                "truncated": True,
            }
        else:
            record_result["data"] = {
                "original_size_bytes": data_size_bytes,
                "truncated": True,
            }
    return record_result


def _local_datetime_from_millis(
    epoch_millis: int | None, zone_offset_seconds: int | None
) -> datetime | None:
    """Render a record instant with its captured fixed offset."""
    instant = datetime_from_millis(epoch_millis)
    if instant is None:
        return None
    return instant.astimezone(timezone(timedelta(seconds=zone_offset_seconds or 0)))


def _sleep_record_data(
    row: HcSleepSessionCurrent[Fetched],
    stages: list[HcSleepStage[Fetched]],
) -> dict[str, object]:
    """Keep compact episode metrics independent from the bounded stage timeline."""
    stage_minutes: dict[str, float] = {}
    time_asleep_minutes = 0.0
    for stage in stages:
        minutes = duration_minutes(stage.start_time, stage.end_time)
        label = render_sleep_stage(stage.stage)
        stage_minutes[label] = stage_minutes.get(label, 0.0) + minutes
        if stage.stage in {2, 4, 5, 6}:
            time_asleep_minutes += minutes
    time_in_bed_minutes = duration_minutes(row.start_time, row.end_time)
    stage_coverage_percent = (
        round(sum(stage_minutes.values()) / time_in_bed_minutes * 100, 2)
        if time_in_bed_minutes > 0
        else 0.0
    )
    return {
        "evidence_uri": (
            f"tether://health-connect/sleep/{row.record_uid}@v{row.version_id}"
        ),
        "notes": row.notes,
        "source_version": row.version_id,
        "stages": [
            {
                "end_time": datetime_from_millis(stage.end_time),
                "stage": stage.stage,
                "stage_label": render_sleep_stage(stage.stage),
                "start_time": datetime_from_millis(stage.start_time),
            }
            for stage in stages[:_TOOL_NESTED_LIMIT]
        ],
        "stages_truncated": len(stages) > _TOOL_NESTED_LIMIT,
        "summary": {
            "local_end": _local_datetime_from_millis(
                row.end_time,
                row.end_zone_offset_seconds or row.start_zone_offset_seconds,
            ),
            "local_start": _local_datetime_from_millis(
                row.start_time, row.start_zone_offset_seconds
            ),
            "sleep_efficiency_percent": (
                round(time_asleep_minutes / time_in_bed_minutes * 100, 2)
                if time_in_bed_minutes > 0
                else None
            ),
            "stage_coverage_percent": stage_coverage_percent,
            "stage_interval_count": len(stages),
            "stage_minutes": {
                label: round(minutes, 2)
                for label, minutes in sorted(stage_minutes.items())
            },
            "stages_complete": stage_coverage_is_complete(stage_coverage_percent),
            "time_asleep_minutes": round(time_asleep_minutes, 2),
            "time_in_bed_minutes": round(time_in_bed_minutes, 2),
        },
        "title": row.title,
    }


@dataclass(frozen=True, slots=True)
class HealthConnectRecordQuery:
    """Read bounded latest non-tombstoned records by Health Connect type."""

    database: Database

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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcExerciseSessionCurrent.end_time.gte(after_millis)
                | (
                    HcExerciseSessionCurrent.end_time.is_null()
                    & HcExerciseSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcExerciseSessionCurrent.start_time.lte(millis_from_datetime(before))
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcGenericRecordCurrent.end_time.gte(after_millis)
                | (
                    HcGenericRecordCurrent.end_time.is_null()
                    & HcGenericRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcGenericRecordCurrent.start_time.lte(millis_from_datetime(before))
            )
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one(query)

    async def _count_current_heart_rates(
        self, *, after: datetime | None, before: datetime | None
    ) -> int:
        """Count heart-rate records matching raw-read bounds."""
        query = select(HcHeartRateRecordCurrent.version_id.count())
        if after is not None:
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcHeartRateRecordCurrent.end_time.gte(after_millis)
                | (
                    HcHeartRateRecordCurrent.end_time.is_null()
                    & HcHeartRateRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcHeartRateRecordCurrent.start_time.lte(millis_from_datetime(before))
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcSleepSessionCurrent.end_time.gte(after_millis)
                | (
                    HcSleepSessionCurrent.end_time.is_null()
                    & HcSleepSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcSleepSessionCurrent.start_time.lte(millis_from_datetime(before))
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcStepIntervalCurrent.end_time.gte(after_millis)
                | (
                    HcStepIntervalCurrent.end_time.is_null()
                    & HcStepIntervalCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcStepIntervalCurrent.start_time.lte(millis_from_datetime(before))
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcStepIntervalCurrent.end_time.gte(after_millis)
                | (
                    HcStepIntervalCurrent.end_time.is_null()
                    & HcStepIntervalCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcStepIntervalCurrent.start_time.lte(millis_from_datetime(before))
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
                end_time=datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=datetime_from_millis(row.modified_at),
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
                received_at=cast("datetime", datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="steps",
                recording_method=row.recording_method,
                start_time=datetime_from_millis(row.start_time),
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcHeartRateRecordCurrent.end_time.gte(after_millis)
                | (
                    HcHeartRateRecordCurrent.end_time.is_null()
                    & HcHeartRateRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcHeartRateRecordCurrent.start_time.lte(millis_from_datetime(before))
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
        samples_by_version: dict[int, list[HcHeartRateSample[Fetched]]] = {}
        for sample in samples:
            samples_by_version.setdefault(sample.version_id, []).append(sample)
        return [
            HealthConnectRecordRead(
                data={
                    "samples": [
                        {
                            "beats_per_minute": sample.beats_per_minute,
                            "time": datetime_from_millis(sample.time),
                        }
                        for sample in samples_by_version.get(row.version_id, [])[
                            :_TOOL_NESTED_LIMIT
                        ]
                    ],
                    "samples_truncated": len(samples_by_version.get(row.version_id, []))
                    > _TOOL_NESTED_LIMIT,
                },
                end_time=datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=datetime_from_millis(row.modified_at),
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
                received_at=cast("datetime", datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="heart_rate",
                recording_method=row.recording_method,
                start_time=datetime_from_millis(row.start_time),
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcSleepSessionCurrent.end_time.gte(after_millis)
                | (
                    HcSleepSessionCurrent.end_time.is_null()
                    & HcSleepSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcSleepSessionCurrent.start_time.lte(millis_from_datetime(before))
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
        stages_by_version: dict[int, list[HcSleepStage[Fetched]]] = {}
        for stage in stages:
            stages_by_version.setdefault(stage.version_id, []).append(stage)
        return [
            HealthConnectRecordRead(
                data=_sleep_record_data(row, stages_by_version.get(row.version_id, [])),
                end_time=datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=datetime_from_millis(row.modified_at),
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
                received_at=cast("datetime", datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="sleep",
                recording_method=row.recording_method,
                start_time=datetime_from_millis(row.start_time),
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcExerciseSessionCurrent.end_time.gte(after_millis)
                | (
                    HcExerciseSessionCurrent.end_time.is_null()
                    & HcExerciseSessionCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcExerciseSessionCurrent.start_time.lte(millis_from_datetime(before))
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
        laps_by_version: dict[int, list[HcExerciseLap[Fetched]]] = {}
        for lap in laps:
            laps_by_version.setdefault(lap.version_id, []).append(lap)
        route_by_version: dict[int, list[HcExerciseRoutePoint[Fetched]]] = {}
        for point in route:
            route_by_version.setdefault(point.version_id, []).append(point)
        segments_by_version: dict[int, list[HcExerciseSegment[Fetched]]] = {}
        for segment in segments:
            segments_by_version.setdefault(segment.version_id, []).append(segment)
        return [
            HealthConnectRecordRead(
                data={
                    "exercise_type": row.exercise_type,
                    "exercise_type_label": render_exercise_type(row.exercise_type),
                    "laps": [
                        {
                            "end_time": datetime_from_millis(lap.end_time),
                            "length_meters": lap.length_meters,
                            "start_time": datetime_from_millis(lap.start_time),
                        }
                        for lap in laps_by_version.get(row.version_id, [])[
                            :_TOOL_NESTED_LIMIT
                        ]
                    ],
                    "nested_truncated": {
                        "laps": len(laps_by_version.get(row.version_id, []))
                        > _TOOL_NESTED_LIMIT,
                        "route": len(route_by_version.get(row.version_id, []))
                        > _TOOL_NESTED_LIMIT,
                        "segments": len(segments_by_version.get(row.version_id, []))
                        > _TOOL_NESTED_LIMIT,
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
                            "time": datetime_from_millis(point.time),
                            "vertical_accuracy_meters": (
                                point.vertical_accuracy_meters
                            ),
                        }
                        for point in route_by_version.get(row.version_id, [])[
                            :_TOOL_NESTED_LIMIT
                        ]
                    ],
                    "segments": [
                        {
                            "end_time": datetime_from_millis(segment.end_time),
                            "repetitions_count": segment.repetitions_count,
                            "segment_type": segment.segment_type,
                            "start_time": datetime_from_millis(segment.start_time),
                        }
                        for segment in segments_by_version.get(row.version_id, [])[
                            :_TOOL_NESTED_LIMIT
                        ]
                    ],
                    "title": row.title,
                },
                end_time=datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=datetime_from_millis(row.modified_at),
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
                received_at=cast("datetime", datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type="exercise",
                recording_method=row.recording_method,
                start_time=datetime_from_millis(row.start_time),
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
            after_millis = millis_from_datetime(after)
            query = query.where(
                HcGenericRecordCurrent.end_time.gte(after_millis)
                | (
                    HcGenericRecordCurrent.end_time.is_null()
                    & HcGenericRecordCurrent.start_time.gte(after_millis)
                )
            )
        if before is not None:
            query = query.where(
                HcGenericRecordCurrent.start_time.lte(millis_from_datetime(before))
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
                end_time=datetime_from_millis(row.end_time),
                end_zone_offset_seconds=row.end_zone_offset_seconds,
                modified_at=datetime_from_millis(row.modified_at),
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
                received_at=cast("datetime", datetime_from_millis(row.received_at)),
                record_id=row.record_uid,
                record_type=record_type,
                recording_method=row.recording_method,
                start_time=datetime_from_millis(row.start_time),
                start_zone_offset_seconds=row.start_zone_offset_seconds,
            )
            for row in rows
        ]
