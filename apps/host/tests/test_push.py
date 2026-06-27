"""Tests for Web Push subscriptions and fired-trigger browser delivery.

Two seams are exercised: the `PushService` directly (subscribe / unsubscribe /
status convergence) against an in-memory DB, and the mounted app through
`TestClient` for the REST surface plus the end-to-end path where a due trigger,
fired by the live scheduler, reaches a connected browser as a `notify` frame.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_true, fixture, load_fixture, test
from starlette.testclient import TestClient

from tether.push import PushService, create_push_schema
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
ENDPOINT = "https://push.example/abc"
PAST = "2000-01-01T00:00:00+00:00"


@fixture
async def push_service() -> AsyncGenerator[PushService]:
    """A fresh, isolated push-subscription database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_push_schema(db)
    yield PushService(database=db)
    await db.close()


@test()
async def subscribe_then_status_reports_one_live_subscription() -> None:
    """Subscribing records a live row that status counts and recognises."""
    service = await load_fixture(push_service())

    _ = await service.subscribe(ENDPOINT, p256dh="key", auth="auth")
    status = await service.status(ENDPOINT)

    assert_eq(status.count, 1)
    assert_true(status.subscribed)


@test()
async def resubscribing_is_idempotent_on_the_endpoint() -> None:
    """Re-subscribing the same endpoint converges on a single live row."""
    service = await load_fixture(push_service())

    _ = await service.subscribe(ENDPOINT, p256dh="k1", auth="a1")
    _ = await service.subscribe(ENDPOINT, p256dh="k2", auth="a2")
    status = await service.status()

    assert_eq(status.count, 1)


@test()
async def unsubscribe_is_convergent() -> None:
    """Unsubscribing removes the row; a second unsubscribe is a no-op."""
    service = await load_fixture(push_service())
    _ = await service.subscribe(ENDPOINT, p256dh="k", auth="a")

    await service.unsubscribe(ENDPOINT)
    await service.unsubscribe(ENDPOINT)
    status = await service.status(ENDPOINT)

    assert_eq(status.count, 0)
    assert_eq(status.subscribed, False)


@test()
async def resubscribe_after_unsubscribe_revives_the_row() -> None:
    """Subscribing again after removal revives the single row, not a duplicate."""
    service = await load_fixture(push_service())
    _ = await service.subscribe(ENDPOINT, p256dh="k", auth="a")
    await service.unsubscribe(ENDPOINT)

    _ = await service.subscribe(ENDPOINT, p256dh="k", auth="a")
    status = await service.status()

    assert_eq(status.count, 1)


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
def push_subscription_round_trips_over_rest() -> None:
    """Subscribe, see status, and unsubscribe through the REST surface."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)

        subscribed = client.post(
            "/api/push/subscriptions",
            json={"endpoint": ENDPOINT, "p256dh": "k", "auth": "a"},
        )
        status = client.get("/api/push/status", params={"endpoint": ENDPOINT})
        unsubscribed = client.request(
            "DELETE",
            "/api/push/subscriptions",
            json={"endpoint": ENDPOINT},
        )
        after = client.get("/api/push/status", params={"endpoint": ENDPOINT})

    assert_eq(subscribed.status_code, 201)
    assert_eq(status.json()["subscribed"], True)
    assert_eq(status.json()["count"], 1)
    assert_eq(unsubscribed.status_code, 200)
    assert_eq(after.json()["subscribed"], False)


@test()
def a_fired_trigger_reaches_the_browser_as_a_notify_frame() -> None:
    """A due fixed-message trigger fired by the scheduler arrives over the WS."""
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

            frame = _receive_until(websocket, "notify")

    assert_eq(frame["body"], "call the dentist")
    assert_eq(frame["trigger_id"], created.json()["id"])


def _receive_until(websocket: Any, frame_type: str) -> dict[str, Any]:
    """Read frames until one of `frame_type` arrives (bounded by WS timeout)."""
    for _attempt in range(50):
        frame = websocket.receive_json()
        if frame.get("type") == frame_type:
            return frame
    message = f"no {frame_type} frame received"
    raise AssertionError(message)
