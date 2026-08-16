"""Health Connect current-projection reads behind the Telemetry seam."""

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_true, test

from tether.health_connect_contracts import (
    HealthConnectBatchRequest,
    HealthConnectRecords,
    RecordMetadata,
    StepsRecord,
)
from tether.health_connect_ingestion import HealthConnectIngestion
from tether.health_connect_persistence import create_health_connect_schema
from tether.health_connect_telemetry import HealthConnectTelemetry


@test()
async def current_record_query_owns_dispatch_count_and_truncation() -> None:
    """One read reports bounded rows and the complete matching count."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    ingestion = HealthConnectIngestion(database)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=("steps",),
        request_id="baseline-request",
        starting_token="starting-token",
    )
    records = [
        StepsRecord(
            count=count,
            end_time=start_time + 60_000,
            end_zone_offset_seconds=0,
            metadata=RecordMetadata(
                client_record_id=None,
                client_record_version=None,
                data_origin_package="android",
                device=None,
                id=f"steps-{index}",
                last_modified_time=start_time + 60_000,
                recording_method=2,
            ),
            start_time=start_time,
            start_zone_offset_seconds=0,
        )
        for index, (count, start_time) in enumerate(
            ((100, 1_700_000_000_000), (200, 1_700_000_100_000)), start=1
        )
    ]
    await ingestion.ingest_batch(
        HealthConnectBatchRequest(
            contract_version=2,
            deletions=[],
            expected_token="starting-token",
            installation_id="pixel-installation",
            mode="baseline",
            next_token="starting-token",
            records=HealthConnectRecords(steps=records),
            record_types=["steps"],
            request_id="page-request",
        )
    )

    current = await HealthConnectTelemetry(database).fetch_records(
        after=None,
        before=None,
        limit=1,
        record_type="steps",
    )

    assert_eq(current.returned_count, 1)
    assert_eq(current.total_matching_count, 2)
    assert_true(current.truncated)
    assert_eq(current.records[0]["record_id"], "steps-2")
    await database.close()
