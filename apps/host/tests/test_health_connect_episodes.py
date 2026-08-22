"""Deterministic Health Connect episode summaries with raw-record provenance."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import structlog
from snekok import Err
from snekql.sqlite import Config, Database, Fetched, select
from snektest import assert_eq, assert_true, test

from tether.health_connect.contracts import (
    AuthoritativeScanRange,
    CompleteHealthConnectBaselineRequest,
    ExerciseLap,
    ExerciseRecord,
    ExerciseSegment,
    HealthConnectBatchRequest,
    HealthConnectDeletion,
    HealthConnectRecords,
    RecordMetadata,
    SleepRecord,
    SleepStage,
)
from tether.health_connect.episodes import (
    EpisodeMaterializeResult,
    HealthEpisodeSummarizer,
)
from tether.health_connect.ingestion import HealthConnectIngestion
from tether.health_connect.persistence import (
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
    create_health_connect_schema,
)

_BASE_MILLIS = 1_700_000_000_000
_HOUR_MILLIS = 3_600_000


def _metadata(record_id: str, modified: int) -> RecordMetadata:
    return RecordMetadata(
        client_record_id=None,
        client_record_version=None,
        data_origin_package="com.example.tracker",
        device=None,
        id=record_id,
        last_modified_time=modified,
        recording_method=2,
    )


def _exercise_record(
    record_id: str,
    *,
    start: int,
    end: int,
    exercise_type: int = 56,
    laps: tuple[ExerciseLap, ...] = (),
) -> ExerciseRecord:
    return ExerciseRecord(
        end_time=end,
        end_zone_offset_seconds=0,
        exercise_type=exercise_type,
        laps=list(laps),
        metadata=_metadata(record_id, end),
        notes=None,
        planned_exercise_session_id=None,
        route=[],
        segments=[
            ExerciseSegment(
                end_time=start + 1_800_000,
                repetitions_count=0,
                segment_type=1,
                start_time=start,
            )
        ],
        start_time=start,
        start_zone_offset_seconds=0,
        title="Morning run",
    )


def _sleep_record(
    record_id: str,
    *,
    stages: tuple[SleepStage, ...],
    start: int,
    end: int,
) -> SleepRecord:
    return SleepRecord(
        end_time=end,
        end_zone_offset_seconds=0,
        metadata=_metadata(record_id, end),
        notes=None,
        stages=list(stages),
        start_time=start,
        start_zone_offset_seconds=0,
        title=None,
    )


async def _seed_ingestion(
    database: Database,
) -> HealthConnectIngestion:
    ingestion = HealthConnectIngestion(database)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=("exercise", "sleep"),
        request_id="baseline-request",
        starting_token="starting-token",
    )
    return ingestion


async def _ingest_baseline(
    ingestion: HealthConnectIngestion,
    *,
    exercise: list[ExerciseRecord],
    sleep: list[SleepRecord],
) -> None:
    outcome = await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[],
            expected_token="starting-token",
            installation_id="pixel-installation",
            mode="baseline",
            next_token="starting-token",
            records=HealthConnectRecords(exercise=exercise, sleep=sleep),
            record_types=["exercise", "sleep"],
            request_id="baseline-page",
        )
    )
    assert_true(not isinstance(outcome, Err))


async def _enter_changes_mode(ingestion: HealthConnectIngestion) -> None:
    outcome = await ingestion.complete_baseline(
        CompleteHealthConnectBaselineRequest(
            baseline_generation=1,
            contract_version=2,
            expected_token="starting-token",
            installation_id="pixel-installation",
            ranges={
                "exercise": AuthoritativeScanRange(
                    end_time=_BASE_MILLIS * 4, start_time=0
                ),
                "sleep": AuthoritativeScanRange(
                    end_time=_BASE_MILLIS * 4, start_time=0
                ),
            },
            record_types=["exercise", "sleep"],
            request_id="completion-request",
        )
    )
    assert_true(not isinstance(outcome, Err))


def _now_after(millis: int) -> datetime:
    return datetime.fromtimestamp((millis + 31 * 60_000) / 1_000, UTC)


async def _fetch_exercise_summaries(
    database: Database,
) -> list[HcExerciseEpisodeSummary[Fetched]]:
    async with database.transaction() as transaction:
        return list(await transaction.fetch_all(select(HcExerciseEpisodeSummary).all()))


async def _fetch_sleep_summaries(
    database: Database,
) -> list[HcSleepEpisodeSummary[Fetched]]:
    async with database.transaction() as transaction:
        return list(await transaction.fetch_all(select(HcSleepEpisodeSummary).all()))


@test()
async def completed_exercise_session_materializes_typed_summary() -> None:
    """One settled exercise session produces one summary row with provenance."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    session_end = session_start + _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[
            _exercise_record(
                "ex-1",
                end=session_end,
                laps=(
                    ExerciseLap(
                        end_time=session_start + 1_800_000,
                        length_meters=1000.0,
                        start_time=session_start,
                    ),
                    ExerciseLap(
                        end_time=session_end,
                        length_meters=500.0,
                        start_time=session_start + 1_800_000,
                    ),
                ),
                start=session_start,
            )
        ],
        sleep=[],
    )

    result = await HealthEpisodeSummarizer(database).materialize(
        now=_now_after(session_end)
    )

    assert_eq(result.exercise_upserts, 1)
    assert_eq(result.sleep_upserts, 0)
    assert_eq(result.invalidations, 0)
    summaries = await _fetch_exercise_summaries(database)
    assert_eq(len(summaries), 1)
    row = summaries[0]
    assert_eq(row.record_uid, "ex-1")
    assert_eq(row.exercise_type, 56)
    assert_eq(row.title, "Morning run")
    assert_eq(row.start_time, session_start)
    assert_eq(row.end_time, session_end)
    assert_eq(row.duration_minutes, 60.0)
    assert_eq(row.segment_count, 1)
    assert_eq(row.lap_count, 2)
    assert_eq(row.total_lap_meters, 1500.0)
    assert_eq(row.processor_version, 1)
    assert_true(row.payload_hash != "")
    assert_true(row.version_id > 0)
    await database.close()


@test()
async def unsettled_session_is_deferred_until_it_settles() -> None:
    """A session that just ended is summarized only after the settle margin."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    session_end = session_start + _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[_exercise_record("ex-1", end=session_end, start=session_start)],
        sleep=[],
    )
    summarizer = HealthEpisodeSummarizer(database)

    early = await summarizer.materialize(
        now=datetime.fromtimestamp(session_end / 1_000, UTC)
    )
    assert_eq(early.exercise_upserts, 0)
    assert_eq(len(await _fetch_exercise_summaries(database)), 0)

    late = await summarizer.materialize(now=_now_after(session_end))
    assert_eq(late.exercise_upserts, 1)
    assert_eq(len(await _fetch_exercise_summaries(database)), 1)
    await database.close()


@test()
async def session_update_recomputes_summary_from_latest_version() -> None:
    """An upstream edit regenerates the summary; raw evidence stays intact."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    original_end = session_start + _HOUR_MILLIS
    edited_end = session_start + 2 * _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[_exercise_record("ex-1", end=original_end, start=session_start)],
        sleep=[],
    )
    summarizer = HealthEpisodeSummarizer(database)
    _ = await summarizer.materialize(now=_now_after(original_end))

    await _enter_changes_mode(ingestion)
    outcome = await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[],
            expected_token="starting-token",
            installation_id="pixel-installation",
            mode="changes",
            next_token="changes-token-1",
            records=HealthConnectRecords(
                exercise=[_exercise_record("ex-1", end=edited_end, start=session_start)]
            ),
            record_types=["exercise", "sleep"],
            request_id="edit-page",
        )
    )
    assert_true(not isinstance(outcome, Err))

    result = await summarizer.materialize(now=_now_after(edited_end))
    assert_eq(result.exercise_upserts, 1)
    summaries = await _fetch_exercise_summaries(database)
    assert_eq(len(summaries), 1)
    row = summaries[0]
    assert_eq(row.end_time, edited_end)
    assert_eq(row.duration_minutes, 120.0)
    await database.close()


@test()
async def tombstone_invalidates_stored_summary() -> None:
    """Deleting the upstream session removes its summary without touching raw rows."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    session_end = session_start + _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[_exercise_record("ex-1", end=session_end, start=session_start)],
        sleep=[],
    )
    summarizer = HealthEpisodeSummarizer(database)
    _ = await summarizer.materialize(now=_now_after(session_end))
    assert_eq(len(await _fetch_exercise_summaries(database)), 1)

    await _enter_changes_mode(ingestion)
    outcome = await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[HealthConnectDeletion(record_id="ex-1", record_type="exercise")],
            expected_token="starting-token",
            installation_id="pixel-installation",
            mode="changes",
            next_token="changes-token-1",
            records=HealthConnectRecords(),
            record_types=["exercise", "sleep"],
            request_id="delete-page",
        )
    )
    assert_true(not isinstance(outcome, Err))

    result = await summarizer.materialize(now=_now_after(session_end))
    assert_eq(result.invalidations, 1)
    assert_eq(len(await _fetch_exercise_summaries(database)), 0)
    await database.close()


@test()
async def rerun_without_new_evidence_is_a_no_op() -> None:
    """Overlapping invocations converge instead of duplicating outputs."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    session_end = session_start + _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[_exercise_record("ex-1", end=session_end, start=session_start)],
        sleep=[
            _sleep_record(
                "slp-1",
                end=session_end,
                stages=(
                    SleepStage(
                        end_time=session_start + _HOUR_MILLIS,
                        stage=5,
                        start_time=session_start,
                    ),
                ),
                start=session_start,
            )
        ],
    )
    now = _now_after(session_end)
    summarizer = HealthEpisodeSummarizer(database)
    first = await summarizer.materialize(now=now)
    assert_eq(first.exercise_upserts, 1)
    assert_eq(first.sleep_upserts, 1)

    second = await summarizer.materialize(now=now + timedelta(hours=1))
    assert_eq(
        second,
        EpisodeMaterializeResult(exercise_upserts=0, invalidations=0, sleep_upserts=0),
    )
    assert_eq(len(await _fetch_exercise_summaries(database)), 1)
    assert_eq(len(await _fetch_sleep_summaries(database)), 1)
    await database.close()


@test()
async def sleep_summary_records_stage_totals() -> None:
    """Deterministic sleep aggregation sums per-stage minutes from raw stages."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    sleep_start = _BASE_MILLIS
    sleep_end = sleep_start + 8 * _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[],
        sleep=[
            _sleep_record(
                "slp-1",
                end=sleep_end,
                stages=(
                    SleepStage(
                        end_time=sleep_start + 30 * 60_000,
                        stage=1,
                        start_time=sleep_start,
                    ),
                    SleepStage(
                        end_time=sleep_start + 3 * _HOUR_MILLIS,
                        stage=4,
                        start_time=sleep_start + 30 * 60_000,
                    ),
                    SleepStage(
                        end_time=sleep_start + 6 * _HOUR_MILLIS,
                        stage=5,
                        start_time=sleep_start + 3 * _HOUR_MILLIS,
                    ),
                    SleepStage(
                        end_time=sleep_end,
                        stage=6,
                        start_time=sleep_start + 6 * _HOUR_MILLIS,
                    ),
                ),
                start=sleep_start,
            )
        ],
    )

    result = await HealthEpisodeSummarizer(database).materialize(
        now=_now_after(sleep_end)
    )

    assert_eq(result.sleep_upserts, 1)
    rows = await _fetch_sleep_summaries(database)
    assert_eq(len(rows), 1)
    row = rows[0]
    assert_eq(row.record_uid, "slp-1")
    assert_eq(row.duration_minutes, 480.0)
    assert_eq(row.minutes_awake, 30.0)
    assert_eq(row.minutes_light, 150.0)
    assert_eq(row.minutes_deep, 180.0)
    assert_eq(row.minutes_rem, 120.0)
    await database.close()


@test()
async def sweep_forever_materializes_settled_sessions_periodically() -> None:
    """The sweep loop summarizes settled sessions and remains cancellable."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = await _seed_ingestion(database)
    session_start = _BASE_MILLIS
    session_end = session_start + _HOUR_MILLIS
    await _ingest_baseline(
        ingestion,
        exercise=[_exercise_record("ex-1", end=session_end, start=session_start)],
        sleep=[],
    )

    summarizer = HealthEpisodeSummarizer(database)
    task = asyncio.create_task(
        summarizer.sweep_forever(
            interval_seconds=0.02,
            logger=structlog.stdlib.get_logger("test.episode_sweep"),
        )
    )
    summaries: list[HcExerciseEpisodeSummary[Fetched]] = []
    try:
        for _ in range(50):
            summaries = await _fetch_exercise_summaries(database)
            if len(summaries) == 1:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert_eq(len(summaries), 1)
    assert_eq(summaries[0].record_uid, "ex-1")
    await database.close()
