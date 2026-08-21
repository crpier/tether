"""Health Connect cursor workflow tests through the ingestion service seam."""

from snekok import Err
from snekql.sqlite import Config, Database, Transaction
from snektest import assert_eq, assert_raises, assert_true, test

from tether.health_connect.contracts import (
    AuthoritativeScanRange,
    CompleteHealthConnectBaselineRequest,
    HealthConnectBatchRequest,
    HealthConnectDeletion,
    HealthConnectRecords,
    HealthRecordType,
)
from tether.health_connect.ingestion import (
    HealthConnectCursorConflict,
    HealthConnectIngestion,
    HealthConnectRecordSink,
    HealthConnectRequestIdentityConflict,
)
from tether.health_connect.persistence import create_health_connect_schema


@test()
async def stale_page_returns_a_typed_failure_without_advancing_cursor() -> None:
    """A delayed page is expected control flow and cannot mutate stream state."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = HealthConnectIngestion(database)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=("steps",),
        request_id="baseline-request",
        starting_token="starting-token",
    )

    outcome = await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[],
            expected_token="stale-token",
            installation_id="pixel-installation",
            mode="baseline",
            next_token="stale-token",
            records=HealthConnectRecords(),
            record_types=["steps"],
            request_id="stale-page",
        )
    )
    state = await ingestion.fetch_sync_state("pixel-installation", ("steps",))

    assert_true(isinstance(outcome, Err))
    if isinstance(outcome, Err):
        assert_true(isinstance(outcome.error, HealthConnectCursorConflict))
    assert_eq(state.current_token, "starting-token")
    await database.close()


@test()
async def changed_replay_returns_request_identity_conflict() -> None:
    """A committed request ID cannot identify different page content."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = HealthConnectIngestion(database)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=("steps",),
        request_id="baseline-request",
        starting_token="starting-token",
    )
    page = HealthConnectBatchRequest(
        contract_version=2,
        deletions=[],
        expected_token="starting-token",
        installation_id="pixel-installation",
        mode="baseline",
        next_token="starting-token",
        records=HealthConnectRecords(),
        record_types=["steps"],
        request_id="page-request",
    )
    _ = await ingestion.ingest_batch(page)

    outcome = await ingestion.ingest_batch(
        page.model_copy(
            update={
                "deletions": [
                    HealthConnectDeletion(record_id="steps-1", record_type="steps")
                ]
            }
        )
    )

    assert_true(isinstance(outcome, Err))
    if isinstance(outcome, Err):
        assert_true(isinstance(outcome.error, HealthConnectRequestIdentityConflict))
    await database.close()


class RecordSinkDefect(Exception):
    """Unexpected defect raised by the test record sink."""


class DefectiveRecordSink:
    """Record sink that fails before a page can commit."""

    async def append_records(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        del transaction, batch, received_at, accepted, skipped
        raise RecordSinkDefect

    async def append_deletions(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        deleted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        del transaction, batch, received_at, deleted, skipped


@test()
async def unexpected_record_sink_defect_leaves_cursor_retryable() -> None:
    """Unexpected persistence defects propagate and roll back cursor progress."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    record_sink: HealthConnectRecordSink = DefectiveRecordSink()
    ingestion = HealthConnectIngestion(database, record_sink=record_sink)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=("steps",),
        request_id="baseline-request",
        starting_token="starting-token",
    )
    _ = await ingestion.complete_baseline(
        CompleteHealthConnectBaselineRequest(
            baseline_generation=1,
            contract_version=2,
            expected_token="starting-token",
            installation_id="pixel-installation",
            ranges={"steps": AuthoritativeScanRange(start_time=0, end_time=0)},
            record_types=["steps"],
            request_id="baseline-completion",
        )
    )
    page = HealthConnectBatchRequest(
        contract_version=2,
        deletions=[],
        expected_token="starting-token",
        installation_id="pixel-installation",
        mode="changes",
        next_token="advanced-token",
        records=HealthConnectRecords(),
        record_types=["steps"],
        request_id="failing-page",
    )

    with assert_raises(RecordSinkDefect):
        _ = await ingestion.ingest_batch(page)
    state = await ingestion.fetch_sync_state("pixel-installation", ("steps",))

    assert_eq(state.current_token, "starting-token")
    await database.close()
