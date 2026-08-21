"""Health Connect current-projection inventory query."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from snekql.sqlite import Database, select

from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.persistence import (
    HcExerciseSessionCurrent,
    HcGenericRecordCurrent,
    HcHeartRateRecordCurrent,
    HcSleepSessionCurrent,
    HcStepIntervalCurrent,
)
from tether.health_connect.telemetry_model import HealthConnectInventoryEntry
from tether.health_connect.telemetry_values import datetime_from_millis, latest_bound


@dataclass(frozen=True, slots=True)
class HealthConnectInventoryQuery:
    """Inventory populated current projections without reading raw history."""

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
                    earliest_start=datetime_from_millis(earliest_start),
                    latest_end=datetime_from_millis(
                        latest_bound(latest_end, latest_start)
                    ),
                    record_count=count,
                    record_type=record_type,
                )
            )
        for record_type, count, earliest_start in generic_rows:
            entries.append(
                HealthConnectInventoryEntry(
                    earliest_start=datetime_from_millis(earliest_start),
                    latest_end=datetime_from_millis(
                        latest_bound(
                            generic_latest_ends[record_type],
                            generic_latest_starts[record_type],
                        )
                    ),
                    record_count=count,
                    record_type=cast("HealthRecordType", record_type),
                )
            )
        return sorted(entries, key=lambda entry: entry.record_type)
