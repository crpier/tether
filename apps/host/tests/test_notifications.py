"""Tests for persisted notifications and fired-trigger persistence.

Two seams are exercised: the `NotificationService` directly (record / list /
dismiss / clear convergence) against an in-memory DB, and the mounted app
through `TestClient` for the REST surface plus the end-to-end path where a due
trigger, fired by the live scheduler, is persisted so a later reload still finds
it.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snekql.sqlite import Config, Database
from snektest import assert_eq, fixture, load_fixture, test
from starlette.testclient import TestClient

from tether.notifications import (
    NotificationDraft,
    NotificationService,
    create_notification_schema,
)
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
PAST = "2000-01-01T00:00:00+00:00"


@fixture
async def notification_service() -> AsyncGenerator[NotificationService]:
    """A fresh, isolated notification database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_notification_schema(db)
    yield NotificationService(database=db)
    await db.close()


@test()
async def recording_then_listing_returns_the_notification_newest_first() -> None:
    """Recorded notifications list newest-first, undismissed only."""
    service = await load_fixture(notification_service())

    _ = await service.record(NotificationDraft(body="first"))
    _ = await service.record(NotificationDraft(body="second"))
    listed = await service.list_recent()

    assert_eq([item.body for item in listed], ["second", "first"])


@test()
async def recording_keeps_the_source_and_action_kind() -> None:
    """A persisted notification carries its producing action and source label."""
    service = await load_fixture(notification_service())

    _ = await service.record(
        NotificationDraft(
            body="the answer",
            trigger_id="trig-1",
            action_kind="prompt",
            source_label="what is the weather?",
        )
    )
    listed = await service.list_recent()

    assert_eq(listed[0].action_kind, "prompt")
    assert_eq(listed[0].source_label, "what is the weather?")
    assert_eq(listed[0].trigger_id, "trig-1")


@test()
async def dismissing_hides_the_notification_and_is_convergent() -> None:
    """Dismissing removes a row from listings; a repeat dismiss is a no-op."""
    service = await load_fixture(notification_service())
    recorded = await service.record(NotificationDraft(body="stand up"))

    await service.dismiss(recorded.id)
    await service.dismiss(recorded.id)
    listed = await service.list_recent()

    assert_eq(listed, [])


@test()
async def clearing_dismisses_every_live_notification() -> None:
    """Clearing dismisses all live rows and reports how many it cleared."""
    service = await load_fixture(notification_service())
    _ = await service.record(NotificationDraft(body="a"))
    _ = await service.record(NotificationDraft(body="b"))

    cleared = await service.clear()
    listed = await service.list_recent()

    assert_eq(cleared, 2)
    assert_eq(listed, [])


def make_client(root: Path, *, tick_seconds: float = 30.0) -> TestClient:
    """Create a test app with isolated persistent DB and `.tether` root."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                scheduler_tick_seconds=tick_seconds,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


@test()
def notifications_round_trip_over_rest() -> None:
    """List reflects a fired notification; dismiss and clear empty the list."""
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), tick_seconds=0.05) as client,
    ):
        login(client)
        with client.websocket_connect("/ws") as websocket:
            created = client.post(
                "/api/triggers",
                json={
                    "recurrence": "once",
                    "action_kind": "message",
                    "payload": "call the dentist",
                    "fire_at": PAST,
                },
            )
            assert_eq(created.status_code, 201)
            _receive_until(websocket, "notify")

        listed = client.get("/api/notifications")
        assert_eq(listed.status_code, 200)
        items = listed.json()
        assert_eq(len(items), 1)
        assert_eq(items[0]["body"], "call the dentist")
        assert_eq(items[0]["action_kind"], "message")
        assert_eq(items[0]["source_label"], "call the dentist")
        assert_eq(items[0]["trigger_id"], created.json()["id"])

        dismissed = client.request("DELETE", f"/api/notifications/{items[0]['id']}")
        assert_eq(dismissed.status_code, 204)
        after_dismiss = client.get("/api/notifications")
        assert_eq(len(after_dismiss.json()), 0)


@test()
def a_fired_notification_survives_a_reload() -> None:
    """A persisted notification is still listed after the socket closes."""
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), tick_seconds=0.05) as client,
    ):
        login(client)
        with client.websocket_connect("/ws") as websocket:
            created = client.post(
                "/api/triggers",
                json={
                    "recurrence": "once",
                    "action_kind": "message",
                    "payload": "drink water",
                    "fire_at": PAST,
                },
            )
            assert_eq(created.status_code, 201)
            _receive_until(websocket, "notify")

        # The socket is closed — mimics a browser that reloads (or was away) and
        # fetches the persisted list fresh.
        reloaded = client.get("/api/notifications")
        assert_eq(reloaded.status_code, 200)
        assert_eq(reloaded.json()[0]["body"], "drink water")

        cleared = client.request("DELETE", "/api/notifications")
        assert_eq(cleared.status_code, 204)
        assert_eq(len(client.get("/api/notifications").json()), 0)


def _receive_until(websocket: Any, frame_type: str) -> dict[str, Any]:
    """Read frames until one of `frame_type` arrives (bounded by WS timeout)."""
    for _attempt in range(50):
        frame = websocket.receive_json()
        if frame.get("type") == frame_type:
            return frame
    message = f"no {frame_type} frame received"
    raise AssertionError(message)
