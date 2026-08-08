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
def query_rejects_an_unbounded_record_limit() -> None:
    """The tool cannot return more than its fixed maximum parent records."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        envelope = call_tool(
            client, "query_health_connect", record_type="steps", limit=11
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
