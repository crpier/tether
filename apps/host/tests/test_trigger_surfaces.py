"""Dual-surface behaviour tests for Scheduled triggers.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation; the `/internal/tools/*` endpoints assert the
uniform envelope. Both derive from `tether.trigger_capabilities`. Triggers are
created far in the future so the live scheduler never fires them mid-test.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_is_none, test
from starlette.testclient import TestClient

from tests.surfaces import call_tool, login, surface_client

FAR_FUTURE = "2099-01-01T15:00:00+00:00"
FAR_PAST = "2020-01-01T15:00:00+00:00"

make_client = surface_client


def create_trigger(client: TestClient, **body: Any) -> dict[str, Any]:
    """Create a trigger through REST and return the response JSON."""
    response = client.post("/api/triggers", json=body)
    assert_eq(response.status_code, 201)
    return response.json()


@test()
def post_creates_a_once_message_trigger() -> None:
    """`POST /api/triggers` creates an active once trigger at the given instant."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        body = create_trigger(
            client,
            recurrence="once",
            action_kind="message",
            payload="call the dentist",
            fire_at=FAR_FUTURE,
        )

    assert_eq(body["recurrence"], "once")
    assert_eq(body["action_kind"], "message")
    assert_eq(body["status"], "active")
    assert_eq(body["next_fire_at"], "2099-01-01T15:00:00Z")
    assert_is_none(body["wall_time"])


@test()
def post_creates_a_daily_prompt_trigger() -> None:
    """A daily agent-prompt trigger stores its wall time and stays active."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        body = create_trigger(
            client,
            recurrence="daily",
            action_kind="prompt",
            payload="summarise my day",
            timezone="UTC",
            time_of_day="09:00",
        )

    assert_eq(body["recurrence"], "daily")
    assert_eq(body["action_kind"], "prompt")
    assert_eq(body["wall_time"], "09:00")
    assert_eq(body["status"], "active")


@test()
def post_rejects_a_weekly_trigger_without_a_weekday() -> None:
    """A malformed time spec is a 422, not a stored row."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/triggers",
            json={
                "recurrence": "weekly",
                "action_kind": "message",
                "payload": "x",
                "timezone": "UTC",
                "time_of_day": "09:00",
            },
        )

    assert_eq(response.status_code, 422)


@test()
def post_rejects_a_once_trigger_in_the_past() -> None:
    """A once trigger whose instant has already passed is a 422, not a stored row."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/triggers",
            json={
                "recurrence": "once",
                "action_kind": "message",
                "payload": "x",
                "fire_at": FAR_PAST,
            },
        )

    assert_eq(response.status_code, 422)


@test()
def get_lists_live_triggers() -> None:
    """`GET /api/triggers` lists created triggers."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = create_trigger(
            client,
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )

        response = client.get("/api/triggers")

    assert_eq(response.status_code, 200)
    listed = response.json()
    assert_in(created["id"], [trigger["id"] for trigger in listed])


@test()
def put_updates_a_trigger_at_its_observed_version() -> None:
    """`PUT` replaces the definition and bumps the version."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = create_trigger(
            client,
            recurrence="once",
            action_kind="message",
            payload="old",
            fire_at=FAR_FUTURE,
        )

        response = client.put(
            f"/api/triggers/{created['id']}",
            json={
                "recurrence": "once",
                "action_kind": "message",
                "payload": "new",
                "fire_at": FAR_FUTURE,
                "version": created["version"],
            },
        )

    assert_eq(response.status_code, 200)
    updated = response.json()
    assert_eq(updated["payload"], "new")
    assert_eq(updated["version"], created["version"] + 1)


@test()
def put_with_a_stale_version_conflicts() -> None:
    """A `PUT` against an out-of-date version is a 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = create_trigger(
            client,
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )
        body = {
            "recurrence": "once",
            "action_kind": "message",
            "payload": "y",
            "fire_at": FAR_FUTURE,
            "version": created["version"],
        }
        _ = client.put(f"/api/triggers/{created['id']}", json=body)

        conflict = client.put(f"/api/triggers/{created['id']}", json=body)

    assert_eq(conflict.status_code, 409)


@test()
def delete_removes_a_trigger() -> None:
    """`DELETE` removes a trigger so it no longer lists."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = create_trigger(
            client,
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )

        deleted = client.delete(
            f"/api/triggers/{created['id']}",
            params={"version": created["version"]},
        )
        listed = client.get("/api/triggers").json()

    assert_eq(deleted.status_code, 200)
    assert_eq([trigger["id"] for trigger in listed], [])


@test()
def delete_of_a_missing_trigger_is_404() -> None:
    """Deleting an unknown id surfaces absence as a 404."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.delete(
            "/api/triggers/018f0000-0000-7000-8000-000000000000",
            params={"version": 1},
        )

    assert_eq(response.status_code, 404)


@test()
def create_trigger_returns_a_well_formed_success_envelope() -> None:
    """A successful create conforms to the envelope and lands active."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
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
        envelope = call_tool(
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

        listed = call_tool(client, "list_triggers")

    assert_eq(listed["result"], [])


@test()
def delete_trigger_removes_it() -> None:
    """Deleting through the tool seam removes the trigger from the list."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        created = call_tool(
            client,
            "create_trigger",
            recurrence="once",
            action_kind="message",
            payload="x",
            fire_at=FAR_FUTURE,
        )["result"]

        envelope = call_tool(
            client,
            "delete_trigger",
            trigger_id=created["id"],
            version=created["version"],
        )
        listed = call_tool(client, "list_triggers")

    assert_eq(envelope["success"], True)
    assert_eq(listed["result"], [])


@test()
def delete_unknown_trigger_is_a_not_found_envelope() -> None:
    """Deleting an unknown id is a well-formed not_found envelope."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "delete_trigger",
            trigger_id="018f0000-0000-7000-8000-000000000000",
            version=1,
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "not_found")
