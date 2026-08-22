"""REST behavior tests for the manual Health Connect dream-now route."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from snekok import Err
from snekql.sqlite import Database
from snektest import assert_eq, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether.app_runtime import AppRuntime, app_runtime
from tether.health_connect.contracts import (
    ExerciseRecord,
    HealthConnectBatchRequest,
    HealthConnectRecords,
    RecordMetadata,
)
from tether.health_connect.episodes import HealthEpisodeSummarizer
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
_BASE_MILLIS = 1_700_000_000_000
_HOUR_MILLIS = 3_600_000


def _make_app(root: Path, *, dreaming_enabled: bool = False) -> Starlette:
    """Create a test app with an isolated database and workspace."""
    return create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret=SESSION_SECRET,
            dreaming_enabled=dreaming_enabled,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
    )


def _login(client: TestClient) -> None:
    """Authenticate the browser session on a scratch test client."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def _telemetry_database(runtime: AppRuntime) -> Database:
    """Return the composed telemetry database for direct seeding."""
    service = runtime.health_distillation_service
    assert service is not None
    return service.telemetry_database


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


async def _seed_summary(runtime: AppRuntime, record_id: str, *, start: int) -> None:
    """Ingest one settled session and materialize its episode summary."""
    telemetry = _telemetry_database(runtime)
    ingestion = runtime.health_connect_ingestion
    record_types = ("exercise",)
    await ingestion.start_baseline(
        installation_id="pixel-installation",
        record_types=record_types,
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
                exercise=[
                    _exercise_record(record_id, start=start, end=start + _HOUR_MILLIS)
                ],
                sleep=[],
            ),
            record_types=list(record_types),
            request_id=f"baseline-page-{record_id}",
        )
    )
    assert not isinstance(outcome, Err), outcome
    result = await HealthEpisodeSummarizer(telemetry).materialize(
        now=datetime.fromtimestamp((start + _HOUR_MILLIS + 31 * 60_000) / 1_000, UTC)
    )
    assert_eq(result.exercise_upserts, 1)


@test()
async def manual_health_dream_now_queues_a_bounded_run() -> None:
    """An authenticated manual trigger queues a run over all new summaries."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            await _seed_summary(runtime, "ex-1", start=_BASE_MILLIS)

            queued = client.post("/api/telemetry/health-connect/dream-now", json={})
            assert_eq(queued.status_code, 200)
            body = queued.json()
            assert_eq(len(body), 1)
            assert_eq(body[0]["status"], "queued")
            assert_eq(body[0]["exercise_through_version_id"], 1)

            # A repeat of the same bounds queues nothing.
            repeat = client.post("/api/telemetry/health-connect/dream-now", json={})
            assert_eq(repeat.status_code, 204)


@test()
async def manual_health_dream_now_period_bounds_the_window() -> None:
    """An explicit period restricts the run to episodes ending inside it."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            await _seed_summary(runtime, "ex-1", start=_BASE_MILLIS)
            start_two = _BASE_MILLIS + 24 * _HOUR_MILLIS
            await _seed_summary(runtime, "ex-2", start=start_two)

            end_one = datetime.fromtimestamp((_BASE_MILLIS + _HOUR_MILLIS) / 1_000, UTC)
            queued = client.post(
                "/api/telemetry/health-connect/dream-now",
                json={
                    "start": (end_one - timedelta(minutes=5)).isoformat(),
                    "end": (end_one + timedelta(minutes=31)).isoformat(),
                },
            )
            assert_eq(queued.status_code, 200)
            body = queued.json()
            assert_eq(len(body), 1)
            assert_eq(body[0]["exercise_since_version_id"], 0)
            assert_eq(body[0]["exercise_through_version_id"], 1)


@test()
async def manual_health_dream_now_disabled_by_default() -> None:
    """Dreaming off leaves the manual seam unavailable."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=False)
        with TestClient(app) as client:
            _login(client)
            denied = client.post("/api/telemetry/health-connect/dream-now", json={})
            assert_eq(denied.status_code, 404)


@test()
async def manual_health_dream_now_rejects_inverted_period() -> None:
    """A period whose start follows its end is a contract error."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            now = datetime.now(UTC)
            inverted = client.post(
                "/api/telemetry/health-connect/dream-now",
                json={
                    "start": (now - timedelta(hours=1)).isoformat(),
                    "end": (now - timedelta(hours=2)).isoformat(),
                },
            )
            assert_eq(inverted.status_code, 422)
