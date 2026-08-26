"""Agent-tool reads over current Health Connect Telemetry projections."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_true, test

from tests.surfaces import call_tool, login, surface_client

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "health_connect"
    / "v2"
    / "representative-batch.json"
)
BASELINE_PATH = "/api/telemetry/health-connect/sync-state/baselines"
BATCH_PATH = "/api/telemetry/health-connect/batches"


def ingest_representative_telemetry(client: Any) -> None:
    """Seed all four typed current projections through the ingestion boundary."""
    response = client.post(
        BASELINE_PATH,
        json={
            "contract_version": 2,
            "installation_id": "pixel-installation",
            "record_types": ["heart_rate", "sleep", "steps", "exercise"],
            "request_id": "baseline-request-1",
            "starting_token": "opaque-starting-token",
        },
    )
    assert_eq(response.status_code, 201)
    response = client.post(BATCH_PATH, json=json.loads(FIXTURE_PATH.read_text()))
    assert_eq(response.status_code, 200)


def ingest_duplicate_step_telemetry(client: Any) -> None:
    """Seed overlapping phone/watch step sources across two local days."""
    response = client.post(
        BASELINE_PATH,
        json={
            "contract_version": 2,
            "installation_id": "duplicate-step-installation",
            "record_types": ["steps"],
            "request_id": "duplicate-step-baseline-request",
            "starting_token": "duplicate-step-token",
        },
    )
    assert_eq(response.status_code, 201)
    response = client.post(
        BATCH_PATH,
        json={
            "contract_version": 2,
            "installation_id": "duplicate-step-installation",
            "record_types": ["steps"],
            "request_id": "duplicate-step-page-request",
            "mode": "baseline",
            "expected_token": "duplicate-step-token",
            "next_token": "duplicate-step-token",
            "records": {
                "steps": [
                    {
                        "metadata": {
                            "id": "phone-steps-day-1",
                            "data_origin_package": "android",
                            "last_modified_time": 1700000000100,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": 1700000000000,
                        "end_time": 1700000060000,
                        "start_zone_offset_seconds": 3600,
                        "end_zone_offset_seconds": 3600,
                        "count": 100,
                    },
                    {
                        "metadata": {
                            "id": "fitbit-steps-day-1",
                            "data_origin_package": "com.fitbit.FitbitMobile",
                            "last_modified_time": 1700000000200,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": 1700000000000,
                        "end_time": 1700000060000,
                        "start_zone_offset_seconds": 3600,
                        "end_zone_offset_seconds": 3600,
                        "count": 98,
                    },
                    {
                        "metadata": {
                            "id": "phone-steps-day-2",
                            "data_origin_package": "android",
                            "last_modified_time": 1700086400100,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": 1700086400000,
                        "end_time": 1700086460000,
                        "start_zone_offset_seconds": 3600,
                        "end_zone_offset_seconds": 3600,
                        "count": 200,
                    },
                    {
                        "metadata": {
                            "id": "fitbit-steps-day-2",
                            "data_origin_package": "com.fitbit.FitbitMobile",
                            "last_modified_time": 1700086400200,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": 1700086400000,
                        "end_time": 1700086460000,
                        "start_zone_offset_seconds": 3600,
                        "end_zone_offset_seconds": 3600,
                        "count": 205,
                    },
                ]
            },
            "deletions": [],
        },
    )
    assert_eq(response.status_code, 200)


def _sleep_stages(
    start_time: int, segments: list[tuple[int, float]]
) -> list[dict[str, int]]:
    """Build contiguous wire stages from independently checked durations."""
    stages: list[dict[str, int]] = []
    cursor = start_time
    for stage, duration_minutes in segments:
        end_time = cursor + int(duration_minutes * 60_000)
        stages.append({"start_time": cursor, "end_time": end_time, "stage": stage})
        cursor = end_time
    return stages


def ingest_sleep_detail_telemetry(client: Any) -> None:
    """Seed adjacent sleep sessions whose stage counts exceed one shared cap."""
    response = client.post(
        BASELINE_PATH,
        json={
            "contract_version": 2,
            "installation_id": "sleep-installation",
            "record_types": ["heart_rate", "sleep"],
            "request_id": "sleep-baseline-request",
            "starting_token": "sleep-token",
        },
    )
    assert_eq(response.status_code, 201)
    nap_start = 1_700_138_580_000
    overnight_start = nap_start - 12 * 60 * 60 * 1_000
    prior_primary_sleeps = [
        (1, 400.0, 40.0, 200.0, 80.0, 80.0, 74),
        (2, 300.0, 30.0, 150.0, 60.0, 60.0, 80),
        (8, 400.0, 40.0, 200.0, 80.0, 80.0, 70),
        (9, 400.0, 40.0, 200.0, 80.0, 80.0, 72),
    ]
    response = client.post(
        BATCH_PATH,
        json={
            "contract_version": 2,
            "installation_id": "sleep-installation",
            "record_types": ["heart_rate", "sleep"],
            "request_id": "sleep-page-request",
            "mode": "baseline",
            "expected_token": "sleep-token",
            "next_token": "sleep-token",
            "records": {
                "heart_rate": [
                    {
                        "metadata": {
                            "id": "sleep-heart-rate",
                            "data_origin_package": "com.fitbit.FitbitMobile",
                            "last_modified_time": nap_start + 171 * 60_000,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": overnight_start,
                        "end_time": nap_start + 171 * 60_000,
                        "start_zone_offset_seconds": 10_800,
                        "end_zone_offset_seconds": 10_800,
                        "samples": [
                            {
                                "time": overnight_start + 60_000,
                                "beats_per_minute": 70,
                            },
                            {
                                "time": overnight_start + 120 * 60_000,
                                "beats_per_minute": 72,
                            },
                            {
                                "time": nap_start + 30 * 60_000,
                                "beats_per_minute": 80,
                            },
                            {
                                "time": nap_start + 120 * 60_000,
                                "beats_per_minute": 82,
                            },
                            {
                                "time": nap_start + 160 * 60_000,
                                "beats_per_minute": 84,
                            },
                            *[
                                {
                                    "time": (
                                        overnight_start + 400 * 60_000 + index * 1_000
                                    ),
                                    "beats_per_minute": 65,
                                }
                                for index in range(51)
                            ],
                        ],
                    },
                    *[
                        {
                            "metadata": {
                                "id": f"primary-heart-rate-{days_ago}",
                                "data_origin_package": "com.fitbit.FitbitMobile",
                                "last_modified_time": overnight_start,
                                "client_record_id": None,
                                "client_record_version": None,
                                "device": None,
                                "recording_method": 2,
                            },
                            "start_time": (
                                overnight_start - days_ago * 24 * 60 * 60 * 1_000
                            ),
                            "end_time": (
                                overnight_start
                                - days_ago * 24 * 60 * 60 * 1_000
                                + int(duration * 60_000)
                            ),
                            "start_zone_offset_seconds": 10_800,
                            "end_zone_offset_seconds": 10_800,
                            "samples": [
                                {
                                    "time": (
                                        overnight_start
                                        - days_ago * 24 * 60 * 60 * 1_000
                                        + 60_000
                                    ),
                                    "beats_per_minute": heart_rate,
                                }
                            ],
                        }
                        for (
                            days_ago,
                            duration,
                            _awake,
                            _light,
                            _deep,
                            _rem,
                            heart_rate,
                        ) in prior_primary_sleeps
                    ],
                ],
                "sleep": [
                    {
                        "metadata": {
                            "id": "latest-nap",
                            "data_origin_package": "com.fitbit.FitbitMobile",
                            "last_modified_time": nap_start + 171 * 60_000,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": nap_start,
                        "end_time": nap_start + 171 * 60_000,
                        "start_zone_offset_seconds": 10_800,
                        "end_zone_offset_seconds": 10_800,
                        "title": "Nap",
                        "notes": None,
                        "stages": _sleep_stages(
                            nap_start,
                            [
                                *((1, minutes) for minutes in (9.0, 9.5)),
                                *((4, 9.4) for _ in range(10)),
                                *((5, 7.1) for _ in range(5)),
                                *((6, 4.6) for _ in range(5)),
                            ],
                        ),
                    },
                    {
                        "metadata": {
                            "id": "adjacent-overnight",
                            "data_origin_package": "com.fitbit.FitbitMobile",
                            "last_modified_time": nap_start,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": overnight_start,
                        "end_time": overnight_start + 384 * 60_000,
                        "start_zone_offset_seconds": 10_800,
                        "end_zone_offset_seconds": 10_800,
                        "title": "Overnight",
                        "notes": None,
                        "stages": _sleep_stages(
                            overnight_start,
                            [
                                *((1, minutes) for minutes in (7.0, 7.0, 7.0, 8.0)),
                                *((4, 7.0) for _ in range(29)),
                                (4, 4.5),
                                *((5, 7.5) for _ in range(9)),
                                (5, 8.0),
                                *((6, 7.0) for _ in range(9)),
                                (6, 9.0),
                            ],
                        ),
                    },
                    *[
                        {
                            "metadata": {
                                "id": f"prior-nap-{days_ago}",
                                "data_origin_package": "com.fitbit.FitbitMobile",
                                "last_modified_time": (
                                    nap_start
                                    - days_ago * 24 * 60 * 60 * 1_000
                                    + 120 * 60_000
                                ),
                                "client_record_id": None,
                                "client_record_version": None,
                                "device": None,
                                "recording_method": 2,
                            },
                            "start_time": (nap_start - days_ago * 24 * 60 * 60 * 1_000),
                            "end_time": (
                                nap_start
                                - days_ago * 24 * 60 * 60 * 1_000
                                + 120 * 60_000
                            ),
                            "start_zone_offset_seconds": 10_800,
                            "end_zone_offset_seconds": 10_800,
                            "title": "Prior nap",
                            "notes": None,
                            "stages": _sleep_stages(
                                nap_start - days_ago * 24 * 60 * 60 * 1_000,
                                [(1, 12.0), (4, 60.0), (5, 30.0), (6, 18.0)],
                            ),
                        }
                        for days_ago in (7, 14)
                    ],
                    *[
                        {
                            "metadata": {
                                "id": f"prior-primary-{days_ago}",
                                "data_origin_package": "com.fitbit.FitbitMobile",
                                "last_modified_time": overnight_start,
                                "client_record_id": None,
                                "client_record_version": None,
                                "device": None,
                                "recording_method": 2,
                            },
                            "start_time": (
                                overnight_start - days_ago * 24 * 60 * 60 * 1_000
                            ),
                            "end_time": (
                                overnight_start
                                - days_ago * 24 * 60 * 60 * 1_000
                                + int(duration * 60_000)
                            ),
                            "start_zone_offset_seconds": 10_800,
                            "end_zone_offset_seconds": 10_800,
                            "title": "Prior primary sleep",
                            "notes": None,
                            "stages": _sleep_stages(
                                overnight_start - days_ago * 24 * 60 * 60 * 1_000,
                                [
                                    (1, awake),
                                    (4, light),
                                    (5, deep),
                                    (6, rem),
                                ],
                            ),
                        }
                        for (
                            days_ago,
                            duration,
                            awake,
                            light,
                            deep,
                            rem,
                            _heart_rate,
                        ) in prior_primary_sleeps
                    ],
                ],
            },
            "deletions": [],
        },
    )
    assert_eq(response.status_code, 200)


def configure_empty_weight_sync(client: Any) -> None:
    """Configure weight synchronization without receiving a current record."""
    response = client.post(
        BASELINE_PATH,
        json={
            "contract_version": 3,
            "installation_id": "empty-scale-installation",
            "record_types": ["weight"],
            "request_id": "empty-weight-baseline-request",
            "starting_token": "empty-weight-token",
        },
    )
    assert_eq(response.status_code, 201)


def ingest_weight_telemetry(client: Any) -> None:
    """Seed one expanded v3 record through the ingestion boundary."""
    response = client.post(
        BASELINE_PATH,
        json={
            "contract_version": 3,
            "installation_id": "scale-installation",
            "record_types": ["weight"],
            "request_id": "weight-baseline-request",
            "starting_token": "weight-token",
        },
    )
    assert_eq(response.status_code, 201)
    response = client.post(
        BATCH_PATH,
        json={
            "contract_version": 3,
            "installation_id": "scale-installation",
            "record_types": ["weight"],
            "request_id": "weight-page-request",
            "mode": "baseline",
            "expected_token": "weight-token",
            "next_token": "weight-token",
            "records": {
                "weight": [
                    {
                        "metadata": {
                            "id": "weight-1",
                            "data_origin_package": "com.example.scale",
                            "last_modified_time": 1700000000100,
                            "client_record_id": None,
                            "client_record_version": None,
                            "device": None,
                            "recording_method": 2,
                        },
                        "start_time": 1700000000000,
                        "end_time": None,
                        "start_zone_offset_seconds": 0,
                        "end_zone_offset_seconds": None,
                        "payload": {
                            "time": 1700000000000,
                            "weight": {"kilograms": 70.5},
                        },
                    }
                ]
            },
            "deletions": [],
        },
    )
    assert_eq(response.status_code, 200)


@test()
def agent_can_inventory_populated_health_connect_record_types() -> None:
    """Inventory reports current counts and UTC bounds, excluding empty types."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(client, "health_connect_inventory")

    assert_eq(envelope["success"], True)
    assert_eq(
        [entry["record_type"] for entry in envelope["result"]],
        ["exercise", "heart_rate", "sleep", "steps"],
    )
    assert_eq(
        envelope["result"][3],
        {
            "record_count": 1,
            "record_type": "steps",
            "earliest_start": "2023-11-14T22:13:20Z",
            "latest_end": "2023-11-14T23:13:20Z",
        },
    )


@test()
def inventory_includes_populated_expanded_record_types() -> None:
    """The inventory discovers populated generic v3 projections too."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_weight_telemetry(client)

        envelope = call_tool(client, "health_connect_inventory")

    assert_eq(
        envelope["result"],
        [
            {
                "earliest_start": "2023-11-14T22:13:20Z",
                "latest_end": "2023-11-14T22:13:20Z",
                "record_count": 1,
                "record_type": "weight",
            }
        ],
    )


@test()
def metric_status_explains_configured_types_with_no_records() -> None:
    """Missing weight data is distinguished from an unconfigured integration."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        configure_empty_weight_sync(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="metric_status",
            record_type="weight",
        )

    assert_eq(
        envelope["result"],
        {
            "explanation": (
                "Weight synchronization is configured, but Health Connect has "
                "provided no current records. The source may have no measurements."
            ),
            "focus": "metric_status",
            "record_count": 0,
            "record_type": "weight",
            "status": "synchronized_no_records",
            "sync_configured": True,
            "supported": True,
        },
    )


@test()
def agent_can_summarize_typed_health_metrics_without_raw_records() -> None:
    """Overview reads return compact aggregate measurements for the time window."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(
            client,
            "summarize_health_connect",
            after="2023-11-14T00:00:00Z",
            before="2023-11-16T00:00:00Z",
        )

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {
            "after": "2023-11-14T00:00:00Z",
            "before": "2023-11-16T00:00:00Z",
            "exercise": {
                "exercise_type_code_counts": {"56": 1},
                "exercise_type_counts": {"running": 1},
                "record_count": 1,
                "total_duration_minutes": 60.0,
            },
            "heart_rate": {
                "average_bpm": 62.0,
                "maximum_bpm": 63,
                "minimum_bpm": 61,
                "record_count": 1,
                "sample_count": 2,
            },
            "other_record_types": [],
            "sleep": {
                "average_duration_minutes": 480.0,
                "record_count": 1,
                "stage_code_duration_minutes": {"4": 60.0},
                "stage_duration_minutes": {"light": 60.0},
                "total_duration_minutes": 480.0,
            },
            "steps": {
                "by_origin": [
                    {
                        "data_origin_package": "com.example.phone",
                        "record_count": 1,
                        "total_count": 1234,
                    }
                ],
                "daily": [],
                "duplicate_source_warning": None,
                "raw_total_count": 1234,
                "record_count": 1,
                "total_count": 1234,
            },
        },
    )


@test()
def summary_deduplicates_overlapping_step_origins_by_local_day() -> None:
    """Daily step summaries prefer one source instead of summing duplicates."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_duplicate_step_telemetry(client)

        envelope = call_tool(
            client,
            "summarize_health_connect",
            after="2023-11-14T00:00:00Z",
            before="2023-11-17T00:00:00Z",
            bucket="day",
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["steps"]["raw_total_count"], 603)
    assert_eq(envelope["result"]["steps"]["total_count"], 305)
    assert_true(envelope["result"]["steps"]["duplicate_source_warning"] is not None)
    assert_eq(
        envelope["result"]["steps"]["by_origin"],
        [
            {
                "data_origin_package": "android",
                "record_count": 2,
                "total_count": 300,
            },
            {
                "data_origin_package": "com.fitbit.FitbitMobile",
                "record_count": 2,
                "total_count": 303,
            },
        ],
    )
    assert_eq(
        envelope["result"]["steps"]["daily"],
        [
            {
                "by_origin": [
                    {
                        "data_origin_package": "android",
                        "record_count": 1,
                        "total_count": 100,
                    },
                    {
                        "data_origin_package": "com.fitbit.FitbitMobile",
                        "record_count": 1,
                        "total_count": 98,
                    },
                ],
                "date": "2023-11-14",
                "duplicate_source_warning": "Multiple step origins overlap; total_count uses the largest origin for this day and raw_total_count is the simple sum.",
                "raw_total_count": 198,
                "record_count": 2,
                "total_count": 100,
            },
            {
                "by_origin": [
                    {
                        "data_origin_package": "android",
                        "record_count": 1,
                        "total_count": 200,
                    },
                    {
                        "data_origin_package": "com.fitbit.FitbitMobile",
                        "record_count": 1,
                        "total_count": 205,
                    },
                ],
                "date": "2023-11-15",
                "duplicate_source_warning": "Multiple step origins overlap; total_count uses the largest origin for this day and raw_total_count is the simple sum.",
                "raw_total_count": 405,
                "record_count": 2,
                "total_count": 205,
            },
        ],
    )


@test()
def raw_queries_report_when_matching_records_are_truncated() -> None:
    """Bounded raw reads tell the agent not to derive totals from partial data."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_duplicate_step_telemetry(client)

        envelope = call_tool(
            client, "query_health_connect", record_type="steps", limit=2
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["returned_count"], 2)
    assert_eq(envelope["result"]["total_matching_count"], 4)
    assert_eq(envelope["result"]["truncated"], True)


@test()
def summary_compacts_generic_numeric_measurements() -> None:
    """Generic payload values become small named series instead of raw records."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_weight_telemetry(client)

        envelope = call_tool(
            client,
            "summarize_health_connect",
            after="2023-11-14T00:00:00Z",
            before="2023-11-16T00:00:00Z",
        )

    assert_eq(
        envelope["result"]["other_record_types"],
        [
            {
                "earliest_start": "2023-11-14T22:13:20Z",
                "latest_end": "2023-11-14T22:13:20Z",
                "numeric_values": [
                    {
                        "average": 70.5,
                        "latest": 70.5,
                        "maximum": 70.5,
                        "minimum": 70.5,
                        "path": "weight.kilograms",
                        "sample_count": 1,
                    }
                ],
                "record_count": 1,
                "record_type": "weight",
            }
        ],
    )


@test()
def agent_can_query_current_steps_within_an_aware_time_window() -> None:
    """A bounded query returns the current interval and its writing origin."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(
            client,
            "query_health_connect",
            record_type="steps",
            after="2023-11-14T22:00:00Z",
            before="2023-11-15T00:00:00Z",
            limit=10,
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["returned_count"], 1)
    assert_eq(envelope["result"]["total_matching_count"], 1)
    assert_eq(envelope["result"]["truncated"], False)
    record = envelope["result"]["records"][0]
    assert_eq(record["record_type"], "steps")
    assert_eq(record["record_id"], "steps-1")
    assert_eq(record["start_time"], "2023-11-14T22:13:20Z")
    assert_eq(record["end_time"], "2023-11-14T23:13:20Z")
    assert_eq(record["start_zone_offset_seconds"], 3600)
    assert_eq(record["data"], {"count": 1234})
    assert_eq(record["origin"]["data_origin_package"], "com.example.phone")


@test()
def query_reads_only_the_latest_accepted_record_version() -> None:
    """A changed upstream record replaces its prior version in agent reads."""
    changed_batch = json.loads(FIXTURE_PATH.read_text())
    changed_batch["request_id"] = "changed-page-request"
    changed_batch["records"]["steps"][0]["count"] = 4321
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)
        response = client.post(BATCH_PATH, json=changed_batch)
        assert_eq(response.status_code, 200)

        envelope = call_tool(client, "query_health_connect", record_type="steps")

    assert_eq(len(envelope["result"]["records"]), 1)
    assert_eq(envelope["result"]["records"][0]["data"], {"count": 4321})


@test()
def query_rejects_a_reversed_time_window() -> None:
    """A backwards aware range fails before reading Telemetry."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "query_health_connect",
            record_type="steps",
            after="2024-01-02T00:00:00Z",
            before="2024-01-01T00:00:00Z",
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def summary_rejects_an_unbounded_time_window() -> None:
    """Overview aggregation cannot scan more than 31 days at once."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "summarize_health_connect",
            after="2024-01-01T00:00:00Z",
            before="2024-02-02T00:00:00Z",
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def query_accepts_the_record_limit_cap() -> None:
    """The tool can return up to fifty parent records when requested."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(
            client, "query_health_connect", record_type="steps", limit=50
        )

    assert_eq(envelope["success"], True)


@test()
def query_rejects_a_record_limit_above_the_cap() -> None:
    """The tool cannot return more than its fixed maximum parent records."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(
            client, "query_health_connect", record_type="steps", limit=51
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def agent_can_read_heart_rate_samples_from_current_records() -> None:
    """Heart-rate queries retain ordered sample instants and measurements."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(client, "query_health_connect", record_type="heart_rate")

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"]["records"][0]["data"]["samples"],
        [
            {"beats_per_minute": 61, "time": "2023-11-14T22:13:21Z"},
            {"beats_per_minute": 63, "time": "2023-11-14T22:13:22Z"},
        ],
    )


@test()
def raw_query_caps_nested_heart_rate_samples() -> None:
    """A raw record query cannot return an unbounded sample stream."""
    changed_batch = json.loads(FIXTURE_PATH.read_text())
    changed_batch["request_id"] = "many-samples-page-request"
    changed_batch["records"]["heart_rate"][0]["samples"] = [
        {"beats_per_minute": 60 + index % 5, "time": 1700000001000 + index}
        for index in range(51)
    ]
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)
        response = client.post(BATCH_PATH, json=changed_batch)
        assert_eq(response.status_code, 200)

        envelope = call_tool(client, "query_health_connect", record_type="heart_rate")

    heart_rate = envelope["result"]["records"][0]["data"]
    assert_eq(len(heart_rate["samples"]), 50)
    assert_eq(heart_rate["samples_truncated"], True)


@test()
def agent_can_read_sleep_sessions_with_ordered_stages() -> None:
    """Sleep queries retain session text and original-enum stage intervals."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(client, "query_health_connect", record_type="sleep")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["records"][0]["data"]["title"], "Night sleep")
    assert_eq(
        envelope["result"]["records"][0]["data"]["notes"],
        "Representative fixture note",
    )
    assert_eq(
        envelope["result"]["records"][0]["data"]["stages"],
        [
            {
                "end_time": "2023-11-14T17:40:00Z",
                "stage": 4,
                "stage_label": "light",
                "start_time": "2023-11-14T16:40:00Z",
            }
        ],
    )


@test()
def adjacent_sleep_sessions_receive_independent_stage_budgets() -> None:
    """A stage-heavy session cannot starve another selected sleep episode."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client, "query_health_connect", record_type="sleep", limit=2
        )

    latest_nap = envelope["result"]["records"][0]
    assert_eq(latest_nap["record_id"], "latest-nap")
    assert_eq(len(latest_nap["data"]["stages"]), 22)
    assert_eq(latest_nap["data"]["stages_truncated"], False)


@test()
def oversized_sleep_timelines_keep_compact_metrics_and_evidence_identity() -> None:
    """Timeline truncation cannot erase a sleep episode's measured summary."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client, "query_health_connect", record_type="sleep", limit=2
        )

    overnight = envelope["result"]["records"][1]["data"]
    assert_eq(
        overnight["evidence_uri"],
        "tether://health-connect/sleep/adjacent-overnight@v2",
    )
    assert_eq(overnight["source_version"], 2)
    assert_eq(
        overnight["summary"],
        {
            "local_end": "2023-11-16T10:07:00+03:00",
            "local_start": "2023-11-16T03:43:00+03:00",
            "sleep_efficiency_percent": 92.45,
            "stage_coverage_percent": 100.0,
            "stage_interval_count": 54,
            "stage_minutes": {
                "awake": 29.0,
                "deep": 75.5,
                "light": 207.5,
                "rem": 72.0,
            },
            "stages_complete": True,
            "time_asleep_minutes": 355.0,
            "time_in_bed_minutes": 384.0,
        },
    )
    assert_eq(overnight["truncated"], True)
    assert_eq(overnight["timeline_omitted"], True)


@test()
def adjacent_heart_rate_records_receive_independent_sample_budgets() -> None:
    """Other selected records cannot consume a heart record's sample budget."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client, "query_health_connect", record_type="heart_rate", limit=5
        )

    latest = envelope["result"]["records"][0]["data"]
    assert_eq(len(latest["samples"]), 50)
    assert_eq(latest["samples_truncated"], True)


@test()
def latest_nap_insight_returns_complete_episode_observations() -> None:
    """One compact read preserves the latest nap's useful measured details."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="sleep_episode",
            episode_kind="nap",
            days=30,
        )

    episode = envelope["result"]["selected_episode"]
    assert_eq(episode["classification"], "nap")
    assert_eq(episode["evidence_uri"], "tether://health-connect/sleep/latest-nap@v1")
    assert_eq(episode["local_start"], "2023-11-16T15:43:00+03:00")
    assert_eq(episode["local_end"], "2023-11-16T18:34:00+03:00")
    assert_eq(episode["time_in_bed_minutes"], 171.0)
    assert_eq(episode["time_asleep_minutes"], 152.5)
    assert_eq(episode["sleep_efficiency_percent"], 89.18)
    assert_eq(
        episode["stage_minutes"],
        {"awake": 18.5, "deep": 35.5, "light": 94.0, "rem": 23.0},
    )
    assert_eq(
        episode["stage_percent_of_time_asleep"],
        {"deep": 23.28, "light": 61.64, "rem": 15.08},
    )
    assert_eq(episode["stage_coverage_percent"], 100.0)
    assert_eq(episode["stage_interval_count"], 22)
    assert_eq(episode["stages_complete"], True)
    assert_eq(
        episode["sleeping_heart_rate"],
        {
            "average_bpm": 82.0,
            "by_stage": {
                "deep": {
                    "average_bpm": 82.0,
                    "maximum_bpm": 82,
                    "minimum_bpm": 82,
                    "sample_count": 1,
                },
                "light": {
                    "average_bpm": 80.0,
                    "maximum_bpm": 80,
                    "minimum_bpm": 80,
                    "sample_count": 1,
                },
                "rem": {
                    "average_bpm": 84.0,
                    "maximum_bpm": 84,
                    "minimum_bpm": 84,
                    "sample_count": 1,
                },
            },
            "maximum_bpm": 84,
            "minimum_bpm": 80,
            "sample_count": 3,
        },
    )


@test()
def latest_nap_insight_compares_with_same_kind_personal_baseline() -> None:
    """A nap comparison uses prior naps and reports its sample size."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="sleep_episode",
            episode_kind="nap",
            days=30,
        )

    assert_eq(
        envelope["result"]["baseline"],
        {
            "classification": "nap",
            "comparison_episode_count": 2,
            "median_sleep_efficiency_percent": 90.0,
            "median_stage_percent_of_time_asleep": {
                "deep": 27.78,
                "light": 55.56,
                "rem": 16.67,
            },
            "median_time_asleep_minutes": 108.0,
            "median_time_in_bed_minutes": 120.0,
            "period_days": 30,
            "selected_delta": {
                "sleep_efficiency_percentage_points": -0.82,
                "stage_percentage_points": {
                    "deep": -4.5,
                    "light": 6.08,
                    "rem": -1.59,
                },
                "time_asleep_minutes": 44.5,
                "time_in_bed_minutes": 51.0,
            },
        },
    )


@test()
def latest_primary_sleep_insight_includes_its_complete_sleep_day() -> None:
    """A last-night read groups the primary sleep with same-day naps."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="sleep_episode",
            episode_kind="primary_sleep",
            days=30,
        )

    assert_eq(
        envelope["result"]["selected_episode"]["record_id"],
        "adjacent-overnight",
    )
    assert_eq(
        envelope["result"]["sleep_day"],
        {
            "date": "2023-11-16",
            "episode_count": 2,
            "evidence_uris": [
                "tether://health-connect/sleep/adjacent-overnight@v2",
                "tether://health-connect/sleep/latest-nap@v1",
            ],
            "nap_count": 1,
            "primary_sleep_count": 1,
            "stage_minutes": {
                "awake": 47.5,
                "deep": 111.0,
                "light": 301.5,
                "rem": 95.0,
            },
            "time_asleep_minutes": 507.5,
            "time_in_bed_minutes": 555.0,
        },
    )


@test()
def sleep_trend_compares_recent_primary_sleep_with_prior_week() -> None:
    """Trend reads compare like-for-like windows and retain daily sleep context."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="sleep_trend",
            days=30,
        )

    assert_eq(
        envelope["result"]["coverage"],
        {
            "nap_count": 3,
            "primary_sleep_count": 5,
            "sleep_day_count": 7,
            "stage_complete_episode_count": 8,
            "total_episode_count": 8,
        },
    )
    assert_eq(
        envelope["result"]["comparison"],
        {
            "current_7_days": {
                "average_sleep_efficiency_percent": 90.82,
                "average_sleeping_heart_rate_bpm": 75.0,
                "average_stage_percent_of_time_asleep": {
                    "deep": 21.9,
                    "light": 56.52,
                    "rem": 21.57,
                },
                "average_time_asleep_minutes": 328.33,
                "end_date": "2023-11-16",
                "primary_sleep_count": 3,
                "start_date": "2023-11-10",
            },
            "previous_7_days": {
                "average_sleep_efficiency_percent": 90.0,
                "average_sleeping_heart_rate_bpm": 71.0,
                "average_stage_percent_of_time_asleep": {
                    "deep": 22.22,
                    "light": 55.56,
                    "rem": 22.22,
                },
                "average_time_asleep_minutes": 360.0,
                "end_date": "2023-11-09",
                "primary_sleep_count": 2,
                "start_date": "2023-11-03",
            },
        },
    )
    latest_day = envelope["result"]["daily"][-1]
    assert_eq(latest_day["date"], "2023-11-16")
    assert_eq(latest_day["nap_count"], 1)
    assert_eq(latest_day["primary_sleep_count"], 1)
    assert_eq(latest_day["time_asleep_minutes"], 507.5)
    assert_eq(latest_day["primary_sleep_efficiency_percent"], 92.45)
    assert_eq(latest_day["primary_sleeping_heart_rate_bpm"], 71.0)


@test()
def sleeping_heart_rate_trend_uses_sleep_aligned_personal_baseline() -> None:
    """Sleeping HR reports aligned observations, coverage, and baseline deltas."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_sleep_detail_telemetry(client)

        envelope = call_tool(
            client,
            "analyze_health_connect",
            focus="sleeping_heart_rate",
            days=30,
        )

    result = envelope["result"]
    assert_eq(
        result["baseline"],
        {
            "comparison_episode_count": 4,
            "latest_difference_bpm": -2.0,
            "median_bpm": 73.0,
        },
    )
    assert_eq(
        result["coverage"],
        {"primary_sleep_count": 5, "with_heart_rate_count": 5},
    )
    assert_eq(
        result["window_comparison"],
        {
            "current_7_days_average_bpm": 75.0,
            "current_7_days_episode_count": 3,
            "difference_bpm": 4.0,
            "previous_7_days_average_bpm": 71.0,
            "previous_7_days_episode_count": 2,
        },
    )
    assert_eq(
        result["observations"][-1],
        {
            "average_bpm": 71.0,
            "date": "2023-11-16",
            "difference_from_baseline_bpm": -2.0,
            "evidence_uri": ("tether://health-connect/sleep/adjacent-overnight@v2"),
            "sample_count": 2,
        },
    )


@test()
def agent_can_read_exercise_sessions_with_nested_details() -> None:
    """Exercise queries retain segment, lap, and route detail with canonical units."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)

        envelope = call_tool(client, "query_health_connect", record_type="exercise")

    assert_eq(envelope["success"], True)
    exercise = envelope["result"]["records"][0]["data"]
    assert_eq(exercise["exercise_type"], 56)
    assert_eq(exercise["exercise_type_label"], "running")
    assert_eq(exercise["segments"][0]["segment_type"], 57)
    assert_eq(exercise["laps"][0]["length_meters"], 1000.5)
    assert_eq(exercise["route"][0]["latitude"], 40.1)
    assert_eq(exercise["route"][0]["time"], "2023-11-14T22:13:21Z")


@test()
def adjacent_exercises_receive_independent_nested_detail_budgets() -> None:
    """Other selected exercises cannot consume a session's segment budget."""
    changed_batch = json.loads(FIXTURE_PATH.read_text())
    changed_batch["request_id"] = "adjacent-exercise-page-request"
    dense = changed_batch["records"]["exercise"][0]
    dense["metadata"]["id"] = "dense-exercise"
    dense["segments"] = [
        {
            "start_time": dense["start_time"] + index * 60_000,
            "end_time": dense["start_time"] + (index + 1) * 60_000,
            "segment_type": 57,
            "repetitions_count": 0,
        }
        for index in range(51)
    ]
    adjacent_exercises: list[dict[str, Any]] = []
    for index in (1, 2):
        adjacent = json.loads(json.dumps(dense))
        adjacent["metadata"]["id"] = f"adjacent-exercise-{index}"
        adjacent["start_time"] += index * 2 * 60 * 60 * 1_000
        adjacent["end_time"] += index * 2 * 60 * 60 * 1_000
        adjacent["segments"] = [
            {
                "start_time": adjacent["start_time"],
                "end_time": adjacent["start_time"] + 60_000,
                "segment_type": 57,
                "repetitions_count": 0,
            }
        ]
        adjacent["laps"] = []
        adjacent["route"] = []
        adjacent_exercises.append(adjacent)
    changed_batch["records"] = {"exercise": [dense, *adjacent_exercises]}
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_representative_telemetry(client)
        response = client.post(BATCH_PATH, json=changed_batch)
        assert_eq(response.status_code, 200)

        envelope = call_tool(
            client, "query_health_connect", record_type="exercise", limit=3
        )

    dense_result = envelope["result"]["records"][2]["data"]
    assert_eq(len(dense_result["segments"]), 50)
    assert_eq(dense_result["nested_truncated"]["segments"], True)


@test()
def raw_query_replaces_oversized_record_data_with_metadata() -> None:
    """One reflected payload cannot inject an unbounded result into agent context."""
    changed_batch = {
        "contract_version": 3,
        "installation_id": "scale-installation",
        "record_types": ["weight"],
        "request_id": "large-weight-page-request",
        "mode": "baseline",
        "expected_token": "weight-token",
        "next_token": "weight-token",
        "records": {
            "weight": [
                {
                    "metadata": {
                        "id": "weight-1",
                        "data_origin_package": "com.example.scale",
                        "last_modified_time": 1700000000200,
                        "client_record_id": None,
                        "client_record_version": None,
                        "device": None,
                        "recording_method": 2,
                    },
                    "start_time": 1700000000000,
                    "end_time": None,
                    "start_zone_offset_seconds": 0,
                    "end_zone_offset_seconds": None,
                    "payload": {"raw": "x" * 5_000, "time": 1700000000000},
                }
            ]
        },
        "deletions": [],
    }
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_weight_telemetry(client)
        response = client.post(BATCH_PATH, json=changed_batch)
        assert_eq(response.status_code, 200)

        envelope = call_tool(client, "query_health_connect", record_type="weight")

    record_data = envelope["result"]["records"][0]["data"]
    assert_eq(record_data["truncated"], True)
    assert_true(record_data["original_size_bytes"] > 4_096)


@test()
def agent_can_read_expanded_v3_payloads_losslessly() -> None:
    """Generic record queries preserve the reflected Health Connect payload."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ingest_weight_telemetry(client)

        envelope = call_tool(client, "query_health_connect", record_type="weight")

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"]["records"][0]["data"],
        {"time": 1700000000000, "weight": {"kilograms": 70.5}},
    )
    assert_eq(
        envelope["result"]["records"][0]["origin"]["data_origin_package"],
        "com.example.scale",
    )
