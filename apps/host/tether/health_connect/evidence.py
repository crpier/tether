"""Resolve exact historical Health Connect episodes for Evidence inspection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from snekql.sqlite import Database, select

from tether.health_connect.persistence import (
    HcExerciseLap,
    HcExerciseSegment,
    HcExerciseSession,
    HcSleepSession,
    HcSleepStage,
)
from tether.health_connect.telemetry_values import (
    datetime_from_millis,
    duration_minutes,
    render_exercise_type,
    render_sleep_stage,
)


@dataclass(frozen=True, slots=True)
class HealthConnectExerciseEvidence:
    """One exact historical Health Connect exercise episode."""

    duration_minutes: float
    end_time: datetime
    exercise_type: str | None
    lap_count: int
    record_uid: str
    segment_count: int
    start_time: datetime
    title: str | None
    total_lap_meters: float | None
    version_id: int


@dataclass(frozen=True, slots=True)
class HealthConnectSleepEvidence:
    """One exact historical Health Connect sleep episode."""

    duration_minutes: float
    end_time: datetime
    record_uid: str
    stage_minutes: dict[str, float]
    start_time: datetime
    title: str | None
    version_id: int


type HealthConnectEvidence = HealthConnectExerciseEvidence | HealthConnectSleepEvidence


class HealthConnectEvidenceResolver:
    """Read deterministic structure from one exact raw episode version."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve(
        self,
        kind: Literal["exercise", "sleep"],
        *,
        record_uid: str,
        version_id: int,
    ) -> HealthConnectEvidence | None:
        if kind == "exercise":
            return await self._resolve_exercise(
                record_uid=record_uid, version_id=version_id
            )
        return await self._resolve_sleep(record_uid=record_uid, version_id=version_id)

    async def _resolve_exercise(
        self, *, record_uid: str, version_id: int
    ) -> HealthConnectExerciseEvidence | None:
        async with self._database.transaction() as transaction:
            episode = await transaction.fetch_one_or_none(
                select(HcExerciseSession)
                .where(HcExerciseSession.record_uid.eq(record_uid))
                .where(HcExerciseSession.version_id.eq(version_id))
            )
            segments = list(
                await transaction.fetch_all(
                    select(HcExerciseSegment).where(
                        HcExerciseSegment.version_id.eq(version_id)
                    )
                )
            )
            laps = list(
                await transaction.fetch_all(
                    select(HcExerciseLap).where(HcExerciseLap.version_id.eq(version_id))
                )
            )
        if (
            episode is None
            or episode.is_deleted
            or episode.start_time is None
            or episode.end_time is None
        ):
            return None
        start_time = datetime_from_millis(episode.start_time)
        end_time = datetime_from_millis(episode.end_time)
        if start_time is None or end_time is None:
            return None
        lap_lengths = [lap.length_meters for lap in laps]
        return HealthConnectExerciseEvidence(
            duration_minutes=duration_minutes(episode.start_time, episode.end_time),
            end_time=end_time,
            exercise_type=render_exercise_type(episode.exercise_type),
            lap_count=len(laps),
            record_uid=episode.record_uid,
            segment_count=len(segments),
            start_time=start_time,
            title=episode.title,
            total_lap_meters=(
                sum(length for length in lap_lengths if length is not None)
                if any(length is not None for length in lap_lengths)
                else None
            ),
            version_id=episode.version_id,
        )

    async def _resolve_sleep(
        self, *, record_uid: str, version_id: int
    ) -> HealthConnectSleepEvidence | None:
        async with self._database.transaction() as transaction:
            episode = await transaction.fetch_one_or_none(
                select(HcSleepSession)
                .where(HcSleepSession.record_uid.eq(record_uid))
                .where(HcSleepSession.version_id.eq(version_id))
            )
            stages = list(
                await transaction.fetch_all(
                    select(HcSleepStage)
                    .where(HcSleepStage.version_id.eq(version_id))
                    .order_by(HcSleepStage.stage_index.asc())
                )
            )
        if (
            episode is None
            or episode.is_deleted
            or episode.start_time is None
            or episode.end_time is None
        ):
            return None
        start_time = datetime_from_millis(episode.start_time)
        end_time = datetime_from_millis(episode.end_time)
        if start_time is None or end_time is None:
            return None
        stage_minutes: dict[str, float] = {}
        for stage in stages:
            label = render_sleep_stage(stage.stage)
            stage_minutes[label] = stage_minutes.get(label, 0.0) + duration_minutes(
                stage.start_time, stage.end_time
            )
        return HealthConnectSleepEvidence(
            duration_minutes=duration_minutes(episode.start_time, episode.end_time),
            end_time=end_time,
            record_uid=episode.record_uid,
            stage_minutes=stage_minutes,
            start_time=start_time,
            title=episode.title,
            version_id=episode.version_id,
        )
