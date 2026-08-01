"""Health Connect HTTP sync contract tests against a real telemetry database."""

import json
import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from httpx2 import Response
from snektest import assert_eq, assert_true, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
API_TOKEN = "test-api-token"
SESSION_SECRET = "test-session-secret"
SYNC_STATE_PATH = "/api/telemetry/health-connect/sync-state"
BASELINE_PATH = f"{SYNC_STATE_PATH}/baselines"
BATCH_PATH = "/api/telemetry/health-connect/batches"
BASELINE_COMPLETE_PATH = f"{BASELINE_PATH}/complete"
FIXTURE_ROOT = Path(__file__).parent / "fixtures/health_connect/v1"
AUTHORIZATION = {"Authorization": f"Bearer {API_TOKEN}"}


def complete_representative_baseline(client: TestClient) -> None:
    """Complete the representative fixture's authoritative baseline ranges."""
    response = client.post(
        BASELINE_COMPLETE_PATH,
        headers=AUTHORIZATION,
        json={
            "contract_version": 1,
            "installation_id": "pixel-installation",
            "record_types": ["heart_rate", "sleep", "steps", "exercise"],
            "request_id": "baseline-complete-1",
            "expected_token": "opaque-starting-token",
            "baseline_generation": 1,
            "ranges": {
                "exercise": {
                    "start_time": 0,
                    "end_time": 2000000000000,
                    "seen_record_ids": ["exercise-1"],
                },
                "heart_rate": {
                    "start_time": 0,
                    "end_time": 2000000000000,
                    "seen_record_ids": ["heart-1"],
                },
                "sleep": {
                    "start_time": 0,
                    "end_time": 2000000000000,
                    "seen_record_ids": ["sleep-1"],
                },
                "steps": {
                    "start_time": 0,
                    "end_time": 2000000000000,
                    "seen_record_ids": ["steps-1"],
                },
            },
        },
    )
    assert_eq(response.status_code, 200)


@contextmanager
def health_connect_client(
    root: Path, *, capture_logs: bool = False
) -> Generator[TestClient]:
    """Run one host with isolated main and telemetry SQLite files."""
    app = create_app(
        config=AppConfig(
            api_token=API_TOKEN,
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            log_file=root / "host.log" if capture_logs else None,
            session_secret=SESSION_SECRET,
            telemetry_database_path=root / "telemetry.sqlite3",
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
    )
    with TestClient(app) as client:
        yield client


@test()
def operational_logs_exclude_health_values_notes_and_complete_tokens() -> None:
    """Batch diagnostics contain safe identity/counts, never sensitive payloads."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root, capture_logs=True) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)
        logs = (root / "host.log").read_text()

    assert_true("page-request-1" in logs)
    assert_true('"accepted"' in logs)
    assert_true("Representative fixture note" not in logs)
    assert_true("opaque-starting-token" not in logs)
    assert_true('"beats_per_minute"' not in logs)


@test()
def sync_state_requires_existing_api_authentication() -> None:
    """Health telemetry routes reject anonymous requests."""
    with (
        TemporaryDirectory() as directory,
        health_connect_client(Path(directory)) as client,
    ):
        response = client.get(
            SYNC_STATE_PATH,
            params={
                "installation_id": "pixel-installation",
                "record_types": "heart_rate,sleep,steps,exercise",
            },
        )

    assert_eq(response.status_code, 401)


@test()
def representative_page_advances_its_cursor_atomically() -> None:
    """One valid page accepts every typed record and advances its stream token."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    with (
        TemporaryDirectory() as directory,
        health_connect_client(Path(directory)) as client,
    ):
        _ = client.post(
            BASELINE_PATH,
            headers=AUTHORIZATION,
            json={
                "contract_version": 1,
                "installation_id": "pixel-installation",
                "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                "request_id": "baseline-request-1",
                "starting_token": "opaque-starting-token",
            },
        )

        response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "accepted": {"exercise": 1, "heart_rate": 1, "sleep": 1, "steps": 1},
            "deleted": {"exercise": 0, "heart_rate": 0, "sleep": 0, "steps": 0},
            "replayed": False,
            "skipped": {"exercise": 0, "heart_rate": 0, "sleep": 0, "steps": 0},
            "status": "accepted",
        },
    )


@test()
def representative_fixture_round_trips_through_typed_current_views() -> None:
    """Every v1 field family remains available from typed current parent/children."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)
        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            heart = database.execute(
                "SELECT record.record_uid, record.modified_at, record.start_zone_offset_seconds, record.recording_method, origin.data_origin_package, origin.device_manufacturer, origin.device_model, origin.device_type FROM hc_heart_rate_record_current record JOIN hc_origin origin ON origin.origin_id = record.origin_id"
            ).fetchone()
            samples = database.execute(
                "SELECT sample_index, time, beats_per_minute FROM hc_heart_rate_sample_current ORDER BY sample_index"
            ).fetchall()
            sleep = database.execute(
                "SELECT title, notes, start_zone_offset_seconds FROM hc_sleep_session_current"
            ).fetchone()
            stages = database.execute(
                "SELECT stage_index, start_time, end_time, stage FROM hc_sleep_stage_current"
            ).fetchall()
            steps = database.execute(
                "SELECT count, modified_at, client_record_id FROM hc_step_interval_current"
            ).fetchone()
            exercise = database.execute(
                "SELECT exercise_type, title, notes, planned_exercise_session_id FROM hc_exercise_session_current"
            ).fetchone()
            segment = database.execute(
                "SELECT segment_index, segment_type, repetitions_count FROM hc_exercise_segment_current"
            ).fetchone()
            lap = database.execute(
                "SELECT lap_index, length_meters FROM hc_exercise_lap_current"
            ).fetchone()
            route = database.execute(
                "SELECT point_index, latitude, longitude, horizontal_accuracy_meters, vertical_accuracy_meters, altitude_meters FROM hc_exercise_route_point_current"
            ).fetchone()

    assert_eq(response.status_code, 200)
    assert_eq(
        heart,
        (
            "heart-1",
            1700000000100,
            -18000,
            1,
            "com.example.watch",
            "Google",
            "Pixel Watch 3",
            2,
        ),
    )
    assert_eq(samples, [(0, 1700000001000, 61), (1, 1700000002000, 63)])
    assert_eq(sleep, ("Night sleep", "Representative fixture note", None))
    assert_eq(stages, [(0, 1699980000000, 1699983600000, 4)])
    assert_eq(steps, (1234, None, None))
    assert_eq(exercise, (56, "Morning run", None, None))
    assert_eq(segment, (0, 57, 2))
    assert_eq(lap, (0, 1000.5))
    assert_eq(route, (0, 40.1, -73.2, 3.2, None, 12.5))


@test()
def a_tombstone_removes_a_record_from_current_without_deleting_history() -> None:
    """Deletion appends history while the heart-rate current view becomes empty."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    deletion = json.loads(json.dumps(batch))
    deletion["request_id"] = "page-request-2"
    deletion["mode"] = "changes"
    deletion["expected_token"] = "opaque-starting-token"
    deletion["next_token"] = "opaque-after-deletion"
    deletion["records"] = {
        "exercise": [],
        "heart_rate": [],
        "sleep": [],
        "steps": [],
    }
    deletion["deletions"] = [{"record_type": "heart_rate", "record_id": "heart-1"}]
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)
            complete_representative_baseline(client)

            response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=deletion)

        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            history_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record WHERE record_uid = ?",
                ("heart-1",),
            ).fetchone()[0]
            current_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record_current WHERE record_uid = ?",
                ("heart-1",),
            ).fetchone()[0]

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["deleted"]["heart_rate"], 1)
    assert_eq(history_count, 2)
    assert_eq(current_count, 0)


@test()
def changed_record_appends_a_new_current_version() -> None:
    """A changed payload appends despite reusing its upstream modified time."""
    baseline = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    changed = json.loads(json.dumps(baseline))
    changed["mode"] = "changes"
    changed["request_id"] = "changed-page"
    changed["expected_token"] = "opaque-starting-token"
    changed["next_token"] = "opaque-changed-token"
    changed["records"]["heart_rate"][0]["samples"][0]["beats_per_minute"] = 72
    changed["records"]["exercise"] = []
    changed["records"]["sleep"] = []
    changed["records"]["steps"] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=baseline)
            complete_representative_baseline(client)

            response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=changed)

        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            version_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record WHERE record_uid = 'heart-1'"
            ).fetchone()[0]
            current_bpm = database.execute(
                "SELECT beats_per_minute FROM hc_heart_rate_sample_current ORDER BY sample_index LIMIT 1"
            ).fetchone()[0]

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["accepted"]["heart_rate"], 1)
    assert_eq(version_count, 2)
    assert_eq(current_bpm, 72)


@test()
def unchanged_record_on_a_new_page_appends_nothing() -> None:
    """Content identity skips overlap even when page request identity is new."""
    baseline = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    overlap = json.loads(json.dumps(baseline))
    overlap["mode"] = "changes"
    overlap["request_id"] = "overlap-page"
    overlap["expected_token"] = "opaque-starting-token"
    overlap["next_token"] = "opaque-overlap-token"
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=baseline)
            complete_representative_baseline(client)

            response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=overlap)

        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            version_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record"
            ).fetchone()[0]

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["accepted"]["heart_rate"], 0)
    assert_eq(response.json()["skipped"]["heart_rate"], 1)
    assert_eq(version_count, 1)


@test()
def lost_response_replay_does_not_append_duplicate_versions() -> None:
    """A committed page request can be retried after its cursor has advanced."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            first = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)

            replay = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)

        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            version_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record"
            ).fetchone()[0]

    assert_eq(first.status_code, 200)
    assert_eq(replay.status_code, 200)
    assert_true(replay.json()["replayed"])
    assert_eq(version_count, 1)


@test()
def malformed_page_leaves_telemetry_and_cursor_unchanged() -> None:
    """Whole-request validation runs before parent rows or cursor mutation."""
    malformed = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    malformed["records"]["heart_rate"][0]["samples"][0]["beats_per_minute"] = 0
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )

            response = client.post(BATCH_PATH, headers=AUTHORIZATION, json=malformed)
            state = client.get(
                SYNC_STATE_PATH,
                headers=AUTHORIZATION,
                params={
                    "installation_id": "pixel-installation",
                    "record_types": "heart_rate,sleep,steps,exercise",
                },
            )
        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            version_count = database.execute(
                "SELECT COUNT(*) FROM hc_heart_rate_record"
            ).fetchone()[0]

    assert_eq(response.status_code, 422)
    assert_eq(version_count, 0)
    assert_eq(state.json()["current_token"], "opaque-starting-token")


@test()
def concurrent_change_pages_allow_only_one_cursor_advance() -> None:
    """Workers racing from one cursor cannot both commit divergent successors."""
    baseline = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    first = json.loads(json.dumps(baseline))
    second = json.loads(json.dumps(baseline))
    for request, request_id, next_token in (
        (first, "concurrent-page-1", "concurrent-next-1"),
        (second, "concurrent-page-2", "concurrent-next-2"),
    ):
        request["mode"] = "changes"
        request["request_id"] = request_id
        request["expected_token"] = "opaque-starting-token"
        request["next_token"] = next_token
        request["records"] = {
            "exercise": [],
            "heart_rate": [],
            "sleep": [],
            "steps": [],
        }
    with (
        TemporaryDirectory() as directory,
        health_connect_client(Path(directory)) as client,
    ):
        _ = client.post(
            BASELINE_PATH,
            headers=AUTHORIZATION,
            json={
                "contract_version": 1,
                "installation_id": "pixel-installation",
                "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                "request_id": "baseline-request-1",
                "starting_token": "opaque-starting-token",
            },
        )
        _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=baseline)
        complete_representative_baseline(client)

        def submit(page: object) -> Response:
            return client.post(BATCH_PATH, headers=AUTHORIZATION, json=page)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, (first, second)))

    assert_eq(sorted(response.status_code for response in responses), [200, 409])


@test()
def stale_change_page_conflicts_without_advancing_the_cursor() -> None:
    """A delayed worker cannot overwrite a newer durable cursor."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    stale = json.loads(json.dumps(batch))
    stale["mode"] = "changes"
    stale["request_id"] = "stale-page"
    stale["expected_token"] = "older-token"
    stale["next_token"] = "wrong-next-token"
    stale["records"] = {"exercise": [], "heart_rate": [], "sleep": [], "steps": []}
    with (
        TemporaryDirectory() as directory,
        health_connect_client(Path(directory)) as client,
    ):
        _ = client.post(
            BASELINE_PATH,
            headers=AUTHORIZATION,
            json={
                "contract_version": 1,
                "installation_id": "pixel-installation",
                "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                "request_id": "baseline-request-1",
                "starting_token": "opaque-starting-token",
            },
        )
        _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)
        complete_representative_baseline(client)

        conflict = client.post(BATCH_PATH, headers=AUTHORIZATION, json=stale)
        state = client.get(
            SYNC_STATE_PATH,
            headers=AUTHORIZATION,
            params={
                "installation_id": "pixel-installation",
                "record_types": "heart_rate,sleep,steps,exercise",
            },
        )

    assert_eq(conflict.status_code, 409)
    assert_eq(state.json()["current_token"], "opaque-starting-token")


@test()
def baseline_reconciliation_preserves_records_outside_authoritative_bounds() -> None:
    """Missing IDs tombstone only where the phone declared an authoritative scan."""
    batch = json.loads((FIXTURE_ROOT / "representative-batch.json").read_text())
    outside = json.loads(json.dumps(batch["records"]["steps"][0]))
    outside["metadata"]["id"] = "steps-outside"
    outside["start_time"] = 1000
    outside["end_time"] = 2000
    batch["records"]["steps"].append(outside)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            _ = client.post(
                BASELINE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-request-1",
                    "starting_token": "opaque-starting-token",
                },
            )
            _ = client.post(BATCH_PATH, headers=AUTHORIZATION, json=batch)
            completion = client.post(
                BASELINE_COMPLETE_PATH,
                headers=AUTHORIZATION,
                json={
                    "contract_version": 1,
                    "installation_id": "pixel-installation",
                    "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                    "request_id": "baseline-complete-1",
                    "expected_token": "opaque-starting-token",
                    "baseline_generation": 1,
                    "ranges": {
                        "exercise": {
                            "start_time": 0,
                            "end_time": 2000000000000,
                            "seen_record_ids": ["exercise-1"],
                        },
                        "heart_rate": {
                            "start_time": 0,
                            "end_time": 2000000000000,
                            "seen_record_ids": ["heart-1"],
                        },
                        "sleep": {
                            "start_time": 0,
                            "end_time": 2000000000000,
                            "seen_record_ids": ["sleep-1"],
                        },
                        "steps": {
                            "start_time": 1600000000000,
                            "end_time": 1800000000000,
                            "seen_record_ids": [],
                        },
                    },
                },
            )
        with closing(sqlite3.connect(root / "telemetry.sqlite3")) as database:
            current_ids = {
                row[0]
                for row in database.execute(
                    "SELECT record_uid FROM hc_step_interval_current"
                ).fetchall()
            }

    assert_eq(completion.status_code, 200)
    assert_eq(completion.json()["deleted"]["steps"], 1)
    assert_eq(current_ids, {"steps-outside"})


@test()
def starting_a_baseline_persists_its_generation_and_starting_token() -> None:
    """A fresh upstream changes token becomes durable before baseline reads."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with health_connect_client(root) as client:
            baseline_request = {
                "contract_version": 1,
                "installation_id": "pixel-installation",
                "record_types": ["heart_rate", "sleep", "steps", "exercise"],
                "request_id": "baseline-request-1",
                "starting_token": "opaque-starting-token",
            }
            started = client.post(
                BASELINE_PATH, headers=AUTHORIZATION, json=baseline_request
            )
            replayed = client.post(
                BASELINE_PATH, headers=AUTHORIZATION, json=baseline_request
            )
        with health_connect_client(root) as restarted_client:
            persisted = restarted_client.get(
                SYNC_STATE_PATH,
                headers=AUTHORIZATION,
                params={
                    "installation_id": "pixel-installation",
                    "record_types": "heart_rate,sleep,steps,exercise",
                },
            )

    assert_eq(started.status_code, 201)
    assert_eq(replayed.status_code, 201)
    assert_eq(persisted.status_code, 200)
    assert_eq(
        persisted.json(),
        {
            "baseline_generation": 1,
            "current_token": "opaque-starting-token",
            "installation_id": "pixel-installation",
            "record_types": ["exercise", "heart_rate", "sleep", "steps"],
            "status": "baseline",
        },
    )


@test()
def a_new_installation_has_initial_sync_state() -> None:
    """A bearer-authenticated phone can discover that baseline work is required."""
    with (
        TemporaryDirectory() as directory,
        health_connect_client(Path(directory)) as client,
    ):
        root = Path(directory)
        response = client.get(
            SYNC_STATE_PATH,
            params={
                "installation_id": "pixel-installation",
                "record_types": "heart_rate,sleep,steps,exercise",
            },
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )

        assert_true((root / "telemetry.sqlite3").exists())

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "baseline_generation": 0,
            "current_token": None,
            "installation_id": "pixel-installation",
            "record_types": ["exercise", "heart_rate", "sleep", "steps"],
            "status": "initial",
        },
    )
