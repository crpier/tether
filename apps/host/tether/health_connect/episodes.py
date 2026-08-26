"""Deterministic Health Connect episode summaries over settled sessions.

Episode summaries are computed structure, not inferred truth: each row is a
pure aggregation of exactly one source session version and carries that
version's identity (`version_id`, `payload_hash`) plus the processor version,
so upstream edits regenerate and tombstones invalidate without mutating raw
telemetry (ADR 0012/0013). Agent interpretation over these summaries is a
separate, later consolidation step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from snekql.sqlite import (
    Database,
    Fetched,
    Transaction,
    delete,
    insert,
    select,
    update,
)

from tether.search_projection.loop import run_reconcile_loop

if TYPE_CHECKING:
    from tether.structured_logging import Logger
from tether.health_connect.persistence import (
    HcEpisodeCursor,
    HcExerciseEpisodeSummary,
    HcExerciseLap,
    HcExerciseSegment,
    HcExerciseSession,
    HcSleepEpisodeSummary,
    HcSleepSession,
    HcSleepStage,
)

_DEFAULT_SETTLE_MARGIN = timedelta(minutes=30)
"""Wall-clock quiet period required after a session's end before summarizing."""

_PROCESSOR_VERSION = 1
"""Bump when aggregation semantics change so stale rows can be recomputed."""

_STAGE_COLUMNS: dict[int, str] = {
    1: "minutes_awake",
    2: "minutes_sleeping",
    3: "minutes_out_of_bed",
    4: "minutes_light",
    5: "minutes_deep",
    6: "minutes_rem",
    7: "minutes_awake_in_bed",
}
"""Health Connect sleep-stage enums mapped to typed summary columns."""


@dataclass(frozen=True, slots=True)
class EpisodeMaterializeResult:
    """Counts produced by one materialization pass across session types."""

    exercise_upserts: int
    sleep_upserts: int
    invalidations: int


def _stage_minutes(stages: list[HcSleepStage[Fetched]]) -> dict[str, float]:
    """Sum per-label minutes; unmapped enum codes degrade into minutes_other."""
    totals: dict[str, float] = dict.fromkeys(_STAGE_COLUMNS.values(), 0.0)
    totals["minutes_other"] = 0.0
    for stage in stages:
        column = _STAGE_COLUMNS.get(stage.stage, "minutes_other")
        totals[column] += max(0.0, (stage.end_time - stage.start_time) / 60_000)
    return totals


def _duration_minutes(start_time: int, end_time: int) -> float:
    return max(0.0, (end_time - start_time) / 60_000)


async def _read_cursor(
    transaction: Transaction, record_type: Literal["exercise", "sleep"]
) -> int:
    row = await transaction.fetch_one_or_none(
        select(HcEpisodeCursor).where(HcEpisodeCursor.record_type.eq(record_type))
    )
    return row.last_version_id if row is not None else 0


async def _write_cursor(
    transaction: Transaction,
    record_type: Literal["exercise", "sleep"],
    *,
    last_version_id: int,
) -> None:
    existing = await transaction.fetch_one_or_none(
        select(HcEpisodeCursor).where(HcEpisodeCursor.record_type.eq(record_type))
    )
    if existing is None:
        _ = await transaction.execute(
            insert(
                HcEpisodeCursor(
                    record_type=record_type, last_version_id=last_version_id
                )
            )
        )
    else:
        _ = await transaction.execute(
            update(HcEpisodeCursor)
            .set(HcEpisodeCursor.last_version_id.to(last_version_id))
            .where(HcEpisodeCursor.record_type.eq(record_type))
        )


async def _advance_cursor_past_resolved(
    transaction: Transaction,
    record_type: Literal["exercise", "sleep"],
    *,
    previous: int,
    resolved_through: int | None,
) -> None:
    """Move the high-water mark to the newest contiguously resolved version."""
    if resolved_through is not None and resolved_through > previous:
        await _write_cursor(transaction, record_type, last_version_id=resolved_through)


@dataclass(frozen=True, slots=True)
class HealthEpisodeSummarizer:
    """Materialize typed deterministic summaries for settled episodes.

    Incremental via a per-type version high-water mark; a version whose
    episode is still unsettled blocks cursor advance so it is reconsidered on
    every subsequent pass until it settles. Reruns without new or changed
    source versions perform no writes.
    """

    database: Database
    settle_margin: timedelta = _DEFAULT_SETTLE_MARGIN

    async def materialize(self, *, now: datetime) -> EpisodeMaterializeResult:
        """Summarize newly settled or changed sessions; converge idempotently."""
        cutoff_millis = int((now - self.settle_margin).timestamp() * 1_000)
        async with self.database.transaction(mode="immediate") as transaction:
            exercise_upserts, exercise_invalidations = await self._materialize_exercise(
                transaction, cutoff_millis=cutoff_millis
            )
            sleep_upserts, sleep_invalidations = await self._materialize_sleep(
                transaction, cutoff_millis=cutoff_millis
            )
        return EpisodeMaterializeResult(
            exercise_upserts=exercise_upserts,
            sleep_upserts=sleep_upserts,
            invalidations=exercise_invalidations + sleep_invalidations,
        )

    async def sweep_forever(
        self, *, interval_seconds: float = 60.0, logger: Logger
    ) -> None:
        """Summarize settled sessions after each interval.

        `materialize` is cursor-based and idempotent. Each pass picks up
        sessions that settled since the prior pass. A failed pass is logged;
        the next tick retries.
        """

        async def _pass() -> EpisodeMaterializeResult:
            return await self.materialize(now=datetime.now(UTC))

        await run_reconcile_loop(
            _pass,
            interval_seconds=interval_seconds,
            initial_delay_seconds=interval_seconds,
            logger=logger,
            failure_message="Health episode summarization failed",
        )

    async def _materialize_exercise(
        self, transaction: Transaction, *, cutoff_millis: int
    ) -> tuple[int, int]:
        previous_cursor = await _read_cursor(transaction, "exercise")
        versions = list(
            await transaction.fetch_all(
                select(HcExerciseSession)
                .where(HcExerciseSession.version_id.gt(previous_cursor))
                .order_by(HcExerciseSession.version_id.asc())
            )
        )
        latest: dict[str, HcExerciseSession[Fetched]] = {}
        for row in versions:
            latest[row.record_uid] = row

        def _resolved(row: HcExerciseSession[Fetched]) -> bool:
            if row.is_deleted:
                return True
            return row.end_time is not None and row.end_time <= cutoff_millis

        blocked = next(
            (
                row.version_id
                for row in versions
                if not _resolved(latest[row.record_uid])
            ),
            None,
        )
        upserts, invalidations = 0, 0
        for source in latest.values():
            existing = await transaction.fetch_one_or_none(
                select(HcExerciseEpisodeSummary).where(
                    HcExerciseEpisodeSummary.record_uid.eq(source.record_uid)
                )
            )
            if source.is_deleted:
                if existing is not None:
                    _ = await transaction.execute(
                        delete(HcExerciseEpisodeSummary).where(
                            HcExerciseEpisodeSummary.record_uid.eq(source.record_uid)
                        )
                    )
                    invalidations += 1
                continue
            if (
                source.start_time is None
                or source.end_time is None
                or source.end_time > cutoff_millis
            ):
                continue
            segments = list(
                await transaction.fetch_all(
                    select(HcExerciseSegment).where(
                        HcExerciseSegment.version_id.eq(source.version_id)
                    )
                )
            )
            laps = list(
                await transaction.fetch_all(
                    select(HcExerciseLap).where(
                        HcExerciseLap.version_id.eq(source.version_id)
                    )
                )
            )
            lap_lengths = [lap.length_meters for lap in laps]
            summary = HcExerciseEpisodeSummary(
                record_uid=source.record_uid,
                version_id=source.version_id,
                payload_hash=source.payload_hash,
                origin_id=source.origin_id,
                exercise_type=source.exercise_type,
                title=source.title,
                start_time=source.start_time,
                end_time=source.end_time,
                duration_minutes=_duration_minutes(source.start_time, source.end_time),
                segment_count=len(segments),
                lap_count=len(laps),
                total_lap_meters=(
                    sum(length for length in lap_lengths if length is not None)
                    if any(length is not None for length in lap_lengths)
                    else None
                ),
                processor_version=_PROCESSOR_VERSION,
            )
            if existing is None:
                _ = await transaction.execute(insert(summary))
            else:
                _ = await transaction.execute(
                    update(HcExerciseEpisodeSummary)
                    .set(
                        HcExerciseEpisodeSummary.version_id.to(summary.version_id),
                        HcExerciseEpisodeSummary.payload_hash.to(summary.payload_hash),
                        HcExerciseEpisodeSummary.origin_id.to(summary.origin_id),
                        HcExerciseEpisodeSummary.exercise_type.to(
                            summary.exercise_type
                        ),
                        HcExerciseEpisodeSummary.title.to(summary.title),
                        HcExerciseEpisodeSummary.start_time.to(summary.start_time),
                        HcExerciseEpisodeSummary.end_time.to(summary.end_time),
                        HcExerciseEpisodeSummary.duration_minutes.to(
                            summary.duration_minutes
                        ),
                        HcExerciseEpisodeSummary.segment_count.to(
                            summary.segment_count
                        ),
                        HcExerciseEpisodeSummary.lap_count.to(summary.lap_count),
                        HcExerciseEpisodeSummary.total_lap_meters.to(
                            summary.total_lap_meters
                        ),
                        HcExerciseEpisodeSummary.processor_version.to(
                            summary.processor_version
                        ),
                    )
                    .where(HcExerciseEpisodeSummary.record_uid.eq(source.record_uid))
                )
            upserts += 1
        resolved_through = (
            blocked - 1
            if blocked is not None
            else versions[-1].version_id
            if versions
            else None
        )
        await _advance_cursor_past_resolved(
            transaction,
            "exercise",
            previous=previous_cursor,
            resolved_through=resolved_through,
        )
        return upserts, invalidations

    async def _materialize_sleep(
        self, transaction: Transaction, *, cutoff_millis: int
    ) -> tuple[int, int]:
        previous_cursor = await _read_cursor(transaction, "sleep")
        versions = list(
            await transaction.fetch_all(
                select(HcSleepSession)
                .where(HcSleepSession.version_id.gt(previous_cursor))
                .order_by(HcSleepSession.version_id.asc())
            )
        )
        latest: dict[str, HcSleepSession[Fetched]] = {}
        for row in versions:
            latest[row.record_uid] = row

        def _resolved(row: HcSleepSession[Fetched]) -> bool:
            if row.is_deleted:
                return True
            return row.end_time is not None and row.end_time <= cutoff_millis

        blocked = next(
            (
                row.version_id
                for row in versions
                if not _resolved(latest[row.record_uid])
            ),
            None,
        )
        upserts, invalidations = 0, 0
        for source in latest.values():
            existing = await transaction.fetch_one_or_none(
                select(HcSleepEpisodeSummary).where(
                    HcSleepEpisodeSummary.record_uid.eq(source.record_uid)
                )
            )
            if source.is_deleted:
                if existing is not None:
                    _ = await transaction.execute(
                        delete(HcSleepEpisodeSummary).where(
                            HcSleepEpisodeSummary.record_uid.eq(source.record_uid)
                        )
                    )
                    invalidations += 1
                continue
            if (
                source.start_time is None
                or source.end_time is None
                or source.end_time > cutoff_millis
            ):
                continue
            stages = list(
                await transaction.fetch_all(
                    select(HcSleepStage).where(
                        HcSleepStage.version_id.eq(source.version_id)
                    )
                )
            )
            minutes = _stage_minutes(stages)
            summary = HcSleepEpisodeSummary(
                record_uid=source.record_uid,
                version_id=source.version_id,
                payload_hash=source.payload_hash,
                origin_id=source.origin_id,
                title=source.title,
                start_time=source.start_time,
                end_time=source.end_time,
                duration_minutes=_duration_minutes(source.start_time, source.end_time),
                minutes_awake=minutes["minutes_awake"],
                minutes_sleeping=minutes["minutes_sleeping"],
                minutes_out_of_bed=minutes["minutes_out_of_bed"],
                minutes_light=minutes["minutes_light"],
                minutes_deep=minutes["minutes_deep"],
                minutes_rem=minutes["minutes_rem"],
                minutes_awake_in_bed=minutes["minutes_awake_in_bed"],
                minutes_other=minutes["minutes_other"],
                processor_version=_PROCESSOR_VERSION,
            )
            if existing is None:
                _ = await transaction.execute(insert(summary))
            else:
                _ = await transaction.execute(
                    update(HcSleepEpisodeSummary)
                    .set(
                        HcSleepEpisodeSummary.version_id.to(summary.version_id),
                        HcSleepEpisodeSummary.payload_hash.to(summary.payload_hash),
                        HcSleepEpisodeSummary.origin_id.to(summary.origin_id),
                        HcSleepEpisodeSummary.title.to(summary.title),
                        HcSleepEpisodeSummary.start_time.to(summary.start_time),
                        HcSleepEpisodeSummary.end_time.to(summary.end_time),
                        HcSleepEpisodeSummary.duration_minutes.to(
                            summary.duration_minutes
                        ),
                        HcSleepEpisodeSummary.minutes_awake.to(summary.minutes_awake),
                        HcSleepEpisodeSummary.minutes_sleeping.to(
                            summary.minutes_sleeping
                        ),
                        HcSleepEpisodeSummary.minutes_out_of_bed.to(
                            summary.minutes_out_of_bed
                        ),
                        HcSleepEpisodeSummary.minutes_light.to(summary.minutes_light),
                        HcSleepEpisodeSummary.minutes_deep.to(summary.minutes_deep),
                        HcSleepEpisodeSummary.minutes_rem.to(summary.minutes_rem),
                        HcSleepEpisodeSummary.minutes_awake_in_bed.to(
                            summary.minutes_awake_in_bed
                        ),
                        HcSleepEpisodeSummary.minutes_other.to(summary.minutes_other),
                        HcSleepEpisodeSummary.processor_version.to(
                            summary.processor_version
                        ),
                    )
                    .where(HcSleepEpisodeSummary.record_uid.eq(source.record_uid))
                )
            upserts += 1
        resolved_through = (
            blocked - 1
            if blocked is not None
            else versions[-1].version_id
            if versions
            else None
        )
        await _advance_cursor_past_resolved(
            transaction,
            "sleep",
            previous=previous_cursor,
            resolved_through=resolved_through,
        )
        return upserts, invalidations
