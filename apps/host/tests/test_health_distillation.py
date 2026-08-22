"""Health consolidation: bounded agent Distillations over episode summaries."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from snekok import Err
from snekql.sqlite import Config, Database, select
from snektest import (
    assert_eq,
    assert_is_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.dreaming import DreamingMutationCoordinator, DreamRunExecutionResult
from tether.dreaming_store import DreamingMutation, create_dreaming_schema
from tether.health_connect.contracts import (
    ExerciseRecord,
    HealthConnectBatchRequest,
    HealthConnectRecords,
    HealthRecordType,
    RecordMetadata,
    SleepRecord,
)
from tether.health_connect.episodes import HealthEpisodeSummarizer
from tether.health_connect.ingestion import HealthConnectIngestion
from tether.health_connect.persistence import (
    HcExerciseEpisodeSummary,
    create_health_connect_schema,
)
from tether.health_distillation import (
    HealthDistillationExecutor,
    HealthDistillationService,
    HealthDreamingWorker,
)
from tether.structured_logging import Logger


def test_logger() -> Logger:
    """Provide a deterministic logger for executor calls."""
    logger: Logger = structlog.get_logger("test.health_distillation")
    return logger


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, run: object, *, logger: Logger) -> DreamRunExecutionResult:
        self.calls += 1
        return DreamRunExecutionResult(status="success", error=None)


class _CurationRunner:
    """Deterministic stand-in for the ephemeral pi curation runner."""

    def __init__(self, response: str) -> None:
        self.response: str = response
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


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


def _exercise_record(record_id: str, *, start: int, end: int) -> ExerciseRecord:
    return ExerciseRecord(
        end_time=end,
        end_zone_offset_seconds=0,
        exercise_type=56,
        laps=[],
        metadata=_metadata(record_id, end),
        notes=None,
        planned_exercise_session_id=None,
        route=[],
        segments=[],
        start_time=start,
        start_zone_offset_seconds=0,
        title="Morning run",
    )


def _sleep_record(record_id: str, *, start: int, end: int) -> SleepRecord:
    return SleepRecord(
        end_time=end,
        end_zone_offset_seconds=0,
        metadata=_metadata(record_id, end),
        notes=None,
        stages=[],
        start_time=start,
        start_zone_offset_seconds=0,
        title=None,
    )


async def _seed_summary(
    telemetry: Database,
    *,
    record_id: str,
    start: int,
    end: int,
    kind: str = "exercise",
) -> None:
    """Ingest one settled session and materialize its episode summary."""
    ingestion = HealthConnectIngestion(telemetry)
    record_types: tuple[HealthRecordType, ...] = ("exercise", "sleep")
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=tuple(record_types),
        request_id=f"baseline-{record_id}",
        starting_token="starting-token",
    )
    outcome = await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[],
            expected_token="starting-token",
            installation_id="pixel-installation",
            mode="baseline",
            next_token="starting-token",
            records=HealthConnectRecords(
                exercise=(
                    [_exercise_record(record_id, start=start, end=end)]
                    if kind == "exercise"
                    else []
                ),
                sleep=(
                    [_sleep_record(record_id, start=start, end=end)]
                    if kind == "sleep"
                    else []
                ),
            ),
            record_types=list(record_types),
            request_id=f"baseline-page-{record_id}",
        )
    )
    assert_true(not isinstance(outcome, Err))
    result = await HealthEpisodeSummarizer(telemetry).materialize(
        now=datetime.fromtimestamp((end + 31 * 60_000) / 1_000, UTC)
    )
    assert_eq(result.exercise_upserts + result.sleep_upserts, 1)


async def _summary_uri(telemetry: Database, record_uid: str) -> str:
    async with telemetry.transaction() as transaction:
        rows = list(
            await transaction.fetch_all(
                select(HcExerciseEpisodeSummary).where(
                    HcExerciseEpisodeSummary.record_uid.eq(record_uid)
                )
            )
        )
    assert len(rows) == 1
    row = rows[0]
    return f"tether://health-connect/exercise/{row.record_uid}@v{row.version_id}"


@fixture
async def health_fixture() -> AsyncGenerator[
    tuple[Database, Database, Path, HealthDistillationService]
]:
    """Isolated tether + telemetry databases with a temporary workspace."""
    tether = await Database.initialize(backend=Config(database=":memory:"))
    telemetry = await Database.initialize(backend=Config(database=":memory:"))
    await create_dreaming_schema(tether)
    await create_health_connect_schema(telemetry)
    with TemporaryDirectory() as workspace:
        yield (
            tether,
            telemetry,
            Path(workspace),
            HealthDistillationService(tether, telemetry),
        )
    await tether.close()
    await telemetry.close()


@test()
async def queued_run_requires_new_summaries_and_advances_monotonically() -> None:
    """Queueing captures current bounds and skips when nothing is new."""
    _, telemetry, _, service = await load_fixture(health_fixture())

    assert_is_none(await service.queue_run())

    session_start = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=session_start,
        end=session_start + _HOUR_MILLIS,
    )

    first = await service.queue_run()
    assert first is not None

    # No new summaries since the queued bounds: a second queue is refused so
    # overlapping windows never double-distill the same evidence.
    assert_is_none(await service.queue_run())


@test()
async def executor_writes_distillation_with_bounded_citations_and_acks() -> None:
    """One run distills bounded summaries into one reviewed Memory document."""
    tether, telemetry, workspace_root, service = await load_fixture(health_fixture())
    session_start = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=session_start,
        end=session_start + _HOUR_MILLIS,
    )
    uri = await _summary_uri(telemetry, "ex-1")
    run = await service.queue_run()
    assert run is not None
    runner = _CurationRunner(
        f"## Health insights\n- Completed a 60 minute morning run. [source]({uri})"
    )
    coordinator = DreamingMutationCoordinator(tether, workspace_root)
    executor = HealthDistillationExecutor(
        telemetry,
        workspace_root,
        mutation_coordinator=coordinator,
        curation_runner=runner,
    )
    worker = HealthDreamingWorker(service, executor, test_logger())

    completed = await worker.run_once()
    assert completed is not None
    assert_eq(completed.status, "success")

    document_path = workspace_root / "health" / f"{run.id}.md"
    assert_true(document_path.exists())
    document = document_path.read_text(encoding="utf-8")
    assert_true("kind: health_distillation" in document)
    assert_true(uri in document)
    assert_true("60 minute morning run" in document)
    async with tether.transaction() as transaction:
        mutations = list(await transaction.fetch_all(select(DreamingMutation).all()))
    assert_eq(len(mutations), 1)
    assert_eq(mutations[0].status, "acknowledged")
    assert_eq(mutations[0].actor, "dream")


@test()
async def no_changes_response_is_a_no_op_without_writes() -> None:
    """A curation pass finding nothing durable leaves no workspace trace."""
    tether, telemetry, workspace_root, service = await load_fixture(health_fixture())
    session_start = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=session_start,
        end=session_start + _HOUR_MILLIS,
    )
    run = await service.queue_run()
    assert run is not None
    executor = HealthDistillationExecutor(
        telemetry,
        workspace_root,
        mutation_coordinator=DreamingMutationCoordinator(tether, workspace_root),
        curation_runner=_CurationRunner("NO_CHANGES"),
    )
    worker = HealthDreamingWorker(service, executor, test_logger())

    completed = await worker.run_once()
    assert completed is not None
    assert_eq(completed.status, "no_op")
    assert_true(not (workspace_root / "health").exists())


@test()
async def citation_outside_the_bounded_window_fails_the_run() -> None:
    """Claims may only cite summaries this bounded run actually consumed."""
    tether, telemetry, workspace_root, service = await load_fixture(health_fixture())
    session_start = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=session_start,
        end=session_start + _HOUR_MILLIS,
    )
    run = await service.queue_run()
    assert run is not None
    executor = HealthDistillationExecutor(
        telemetry,
        workspace_root,
        mutation_coordinator=DreamingMutationCoordinator(tether, workspace_root),
        curation_runner=_CurationRunner(
            "## Health insights\n- Claimed. [source](tether://health-connect/exercise/unknown@v99)"
        ),
    )
    worker = HealthDreamingWorker(service, executor, test_logger())

    completed = await worker.run_once()
    assert completed is not None
    assert_eq(completed.status, "failed")
    assert_true(completed.error is not None and "unknown@v99" in completed.error)
    assert_true(not (workspace_root / "health").exists())


@test()
async def resume_settles_a_prior_unacknowledged_mutation_without_rerunning() -> None:
    """Crash recovery retries notification only; curation does not rerun."""
    tether, telemetry, workspace_root, service = await load_fixture(health_fixture())
    session_start = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=session_start,
        end=session_start + _HOUR_MILLIS,
    )
    uri = await _summary_uri(telemetry, "ex-1")
    run = await service.queue_run()
    assert run is not None
    coordinator = DreamingMutationCoordinator(tether, workspace_root)
    document_path = workspace_root / "health" / f"{run.id}.md"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text(
        f"---\ntitle: Health insights\nkind: health_distillation\nrun_id: {run.id}\nevidence:\n- {uri}\n---\n\n## Health insights\n- Prior body. [source]({uri})\n",
        encoding="utf-8",
    )
    tool_call_id = HealthDistillationExecutor.mutation_tool_call_id(run)
    _ = await coordinator.record_mutation(
        run_id=run.id,
        tool_call_id=tool_call_id,
        actor="dream",
        operation="write",
        workspace_path=document_path,
        payload="prior",
    )
    runner = _CurationRunner("SHOULD_NOT_BE_CALLED")
    executor = HealthDistillationExecutor(
        telemetry,
        workspace_root,
        mutation_coordinator=coordinator,
        curation_runner=runner,
    )
    worker = HealthDreamingWorker(service, executor, test_logger())

    completed = await worker.run_once()
    assert completed is not None
    assert_eq(completed.status, "success")
    assert_eq(runner.prompts, [])
    async with tether.transaction() as transaction:
        mutations = list(await transaction.fetch_all(select(DreamingMutation).all()))
    assert_eq(len(mutations), 1)
    assert_eq(mutations[0].attempts, 2)
    assert_eq(mutations[0].status, "acknowledged")


@test()
async def worker_drains_multiple_queued_runs_in_order() -> None:
    """The loop settles every queued run before idling."""
    tether, telemetry, workspace_root, service = await load_fixture(health_fixture())
    start_one = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=start_one,
        end=start_one + _HOUR_MILLIS,
    )
    start_two = _BASE_MILLIS + 24 * _HOUR_MILLIS
    first_run = await service.queue_run()
    assert first_run is not None
    await _seed_summary(
        telemetry,
        record_id="ex-2",
        start=start_two,
        end=start_two + _HOUR_MILLIS,
    )
    runner_prompts: list[list[str]] = []

    class _RecordingRunner:
        async def run(self, prompt: str) -> str:
            runner_prompts.append(prompt.splitlines())
            return "NO_CHANGES"

    second_run = await service.queue_run()
    assert second_run is not None

    executor = HealthDistillationExecutor(
        telemetry,
        workspace_root,
        mutation_coordinator=DreamingMutationCoordinator(tether, workspace_root),
        curation_runner=_RecordingRunner(),
    )
    worker = HealthDreamingWorker(service, executor, test_logger())

    first = await worker.run_once()
    assert first is not None
    second = await worker.run_once()
    assert second is not None
    assert_is_none(await worker.run_once())
    assert_eq(len(runner_prompts), 2)
    # The second run must see strictly newer summaries than the first.
    first_uris = [
        line for line in runner_prompts[0] if line.startswith("uri: tether://")
    ]
    second_uris = [
        line for line in runner_prompts[1] if line.startswith("uri: tether://")
    ]
    assert_true(first_uris == ["uri: tether://health-connect/exercise/ex-1@v1"])
    assert_true(second_uris == ["uri: tether://health-connect/exercise/ex-2@v2"])


@test()
async def health_worker_waits_one_poll_interval_before_claiming_at_startup() -> None:
    """Startup leaves the Health worker cancellable before its first DB transaction."""
    _, telemetry, _, service = await load_fixture(health_fixture())
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=_BASE_MILLIS,
        end=_BASE_MILLIS + _HOUR_MILLIS,
    )
    run = await service.queue_run()
    assert run is not None
    executor = _RecordingExecutor()
    worker = HealthDreamingWorker(
        service,
        executor,
        test_logger(),
        poll_interval_seconds=0.1,
    )

    task = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.sleep(0.02)
        assert_eq(executor.calls, 0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@test()
async def explicit_window_queues_a_run_bounded_to_the_period() -> None:
    """A manual request distills only episodes ending inside the period."""
    _, telemetry, _, service = await load_fixture(health_fixture())
    start_one = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=start_one,
        end=start_one + _HOUR_MILLIS,
    )
    start_two = _BASE_MILLIS + 24 * _HOUR_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-2",
        start=start_two,
        end=start_two + _HOUR_MILLIS,
    )
    end_one_ms = start_one + _HOUR_MILLIS

    run = await service.queue_explicit_run(
        start=datetime.fromtimestamp((end_one_ms - 60_000) / 1_000, UTC),
        end=datetime.fromtimestamp((end_one_ms + 31 * 60_000) / 1_000, UTC),
    )
    assert run is not None
    assert_eq(run.exercise_since_version_id, 0)
    assert_eq(run.exercise_through_version_id, 1)

    # A period covering both episodes yields the full window.
    both = await service.queue_explicit_run(
        start=datetime.fromtimestamp(start_one / 1_000, UTC),
        end=datetime.fromtimestamp((start_two + _HOUR_MILLIS) / 1_000, UTC),
    )
    assert both is not None
    assert_eq(both.exercise_through_version_id, 2)


@test()
async def explicit_window_refuses_empty_and_repeated_periods() -> None:
    """No episodes in the period, or a repeat, queues nothing."""
    _, telemetry, _, service = await load_fixture(health_fixture())
    start_one = _BASE_MILLIS
    await _seed_summary(
        telemetry,
        record_id="ex-1",
        start=start_one,
        end=start_one + _HOUR_MILLIS,
    )
    before_anything = await service.queue_explicit_run(
        start=datetime.fromtimestamp(_BASE_MILLIS / 1_000, UTC) - timedelta(days=7),
        end=datetime.fromtimestamp(_BASE_MILLIS / 1_000, UTC) - timedelta(days=6),
    )
    assert_is_none(before_anything)

    period_start = datetime.fromtimestamp(start_one / 1_000, UTC)
    period_end = datetime.fromtimestamp((start_one + _HOUR_MILLIS) / 1_000, UTC)
    first = await service.queue_explicit_run(start=period_start, end=period_end)
    assert first is not None
    repeat = await service.queue_explicit_run(start=period_start, end=period_end)
    assert_is_none(repeat)


@test()
async def queue_run_chunks_large_backlogs_into_bounded_windows() -> None:
    """A big uncaptured backlog queues successive capped runs, not one mega-run."""
    tether, telemetry, _, _ = await load_fixture(health_fixture())
    capped = HealthDistillationService(
        tether,
        telemetry,
        max_summaries_per_run=2,
    )
    starts = [_BASE_MILLIS + offset * 24 * _HOUR_MILLIS for offset in range(5)]
    for index, session_start in enumerate(starts):
        await _seed_summary(
            telemetry,
            record_id=f"ex-{index + 1}",
            start=session_start,
            end=session_start + _HOUR_MILLIS,
        )

    first = await capped.queue_run()
    assert first is not None
    assert_eq(first.exercise_since_version_id, 0)
    assert_eq(first.exercise_through_version_id, 2)

    second = await capped.queue_run()
    assert second is not None
    assert_eq(second.exercise_since_version_id, 2)
    assert_eq(second.exercise_through_version_id, 4)

    third = await capped.queue_run()
    assert third is not None
    assert_eq(third.exercise_since_version_id, 4)
    assert_eq(third.exercise_through_version_id, 5)

    assert_is_none(await capped.queue_run())


@test()
async def scan_drains_a_backlog_multiple_chunks_per_tick() -> None:
    """One scan tick advances several chunks so backlogs catch up quickly."""
    tether, telemetry, _, _ = await load_fixture(health_fixture())
    capped = HealthDistillationService(
        tether,
        telemetry,
        max_summaries_per_run=1,
    )
    starts = [_BASE_MILLIS + offset * 24 * _HOUR_MILLIS for offset in range(4)]
    for index, session_start in enumerate(starts):
        await _seed_summary(
            telemetry,
            record_id=f"ex-{index + 1}",
            start=session_start,
            end=session_start + _HOUR_MILLIS,
        )

    drained = await capped.drain_backlog()
    assert_eq(len(drained), 4)
    assert_eq(drained[0].exercise_through_version_id, 1)
    assert_eq(drained[-1].exercise_through_version_id, 4)
