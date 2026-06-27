"""Behaviour tests for the loopback internal Scheduled-trigger tool surface.

These drive the mounted Starlette app through `TestClient`, calling the
`/internal/tools/*` trigger endpoints directly — no LLM, no pi. They share the
auth gate and uniform envelope with the other tools; here we assert the
trigger-specific behaviour: create / list / delete and malformed-spec handling.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"
FAR_FUTURE = "2099-01-01T15:00:00+00:00"


def make_client(root: Path) -> TestClient:
    """A test app with an isolated DB/KB, a known tool secret, one session."""
    app = create_app(
        config=AppConfig(
            app_password="test-app-password",
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret="test-session-secret",
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        tool_secret=SECRET,
    )
    cast("SessionRegistry", app.state.session_registry).register(SESSION)
    return TestClient(app)


def call(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke a tool with the known secret and session, returning the envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


@test()
def create_trigger_returns_a_well_formed_success_envelope() -> None:
    """A successful create conforms to the envelope and lands active."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
            client,
            "create_trigger",
            recurrence="once",
            action_kind="message",
            payload="call the dentist",
            fire_at=FAR_FUTURE,
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["status"], "active")
    assert_eq(envelope["result"]["action_kind"], "message")
    assert_is_none(envelope["quota"])


@test()
def create_trigger_with_a_bad_spec_is_a_success_false_envelope() -> None:
    """A weekly trigger missing its weekday is a well-formed invalid_input."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
            client,
            "create_trigger",
            recurrence="weekly",
            action_kind="message",
            payload="x",
            timezone="UTC",
            time_of_day="09:00",
        )

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
        assert_is_none(envelope["result"])

        listed = call(client, "list_triggers")

    assert_eq(listed["result"], [])


@test()
def list_triggers_returns_created_triggers() -> None:
    """The list tool returns triggers created through the tool seam."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        created = call(
            client,
            "create_trigger",
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )

        listed = call(client, "list_triggers")

    assert_in(created["result"]["id"], [t["id"] for t in listed["result"]])


@test()
def delete_trigger_removes_it() -> None:
    """Deleting through the tool seam removes the trigger from the list."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        created = call(
            client,
            "create_trigger",
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )["result"]

        envelope = call(
            client,
            "delete_trigger",
            trigger_id=created["id"],
            version=created["version"],
        )
        listed = call(client, "list_triggers")

    assert_eq(envelope["success"], True)
    assert_eq(listed["result"], [])


@test()
def delete_unknown_trigger_is_a_not_found_envelope() -> None:
    """Deleting an unknown id is a well-formed not_found envelope."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
            client,
            "delete_trigger",
            trigger_id="018f0000-0000-7000-8000-000000000000",
            version=1,
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "not_found")
