"""Dual-surface behaviour tests for Todos.

One app, both shells: the REST routes assert request parsing, status codes, and
response serialisation; the `/internal/tools/*` endpoints assert the chat tool
spellings and the uniform envelope. Both derive from `tether.todo_capabilities`,
so shared service behaviour (creation, the ready/waiting split, status
transitions) is exercised once through whichever shell states it most directly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_false, assert_in, assert_true, test
from starlette.testclient import TestClient

from tests.surfaces import call_tool, login, surface_client

make_client = surface_client


def create_todo(client: TestClient, action: str, **params: Any) -> dict[str, Any]:
    """Create a Todo through the tool surface, returning its result envelope."""
    envelope = call_tool(client, "create_todo", action=action, **params)
    assert_true(envelope["success"])
    return envelope["result"]


@test()
def create_todo_tool_makes_an_active_ready_todo() -> None:
    """`create_todo` makes an active Todo that lists as ready."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        todo = create_todo(client, "call the dentist")
        assert_eq(todo["status"], "active")
        assert_false(todo["waiting"])

        listing = client.get("/api/todos")
        assert_eq(listing.status_code, 200)
        body = listing.json()
        ready_actions = [item["action"] for item in body["ready"]]
        assert_in("call the dentist", ready_actions)
        assert_eq(body["waiting"], [])


@test()
def a_condition_lists_the_todo_as_waiting() -> None:
    """A Todo created with a condition surfaces under waiting, not ready."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        _ = create_todo(client, "bring the book", condition="next time I visit Ana")

        body = client.get("/api/todos").json()
        assert_eq(body["ready"], [])
        waiting = body["waiting"]
        assert_eq(len(waiting), 1)
        assert_eq(waiting[0]["condition"], "next time I visit Ana")
        assert_true(waiting[0]["waiting"])


@test()
def set_status_rest_route_transitions_a_todo() -> None:
    """`POST /api/todos/{id}/status` transitions a Todo at its version."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        todo = create_todo(client, "water plants")

        response = client.post(
            f"/api/todos/{todo['id']}/status",
            json={"status": "completed", "version": todo["version"]},
        )
        assert_eq(response.status_code, 200)
        assert_eq(response.json()["status"], "completed")

        # A completed Todo leaves the active readiness listing.
        body = client.get("/api/todos").json()
        assert_eq(body["ready"], [])


@test()
def a_stale_version_status_transition_is_a_409() -> None:
    """A status transition at a stale version is an optimistic-concurrency 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        todo = create_todo(client, "water plants")
        first = client.post(
            f"/api/todos/{todo['id']}/status",
            json={"status": "completed", "version": todo["version"]},
        )
        assert_eq(first.status_code, 200)
        stale = client.post(
            f"/api/todos/{todo['id']}/status",
            json={"status": "abandoned", "version": todo["version"]},
        )
        assert_eq(stale.status_code, 409)


@test()
def a_status_transition_on_an_absent_todo_is_a_404() -> None:
    """A status transition on a missing Todo is a 404."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/todos/018f0000-0000-7000-8000-0000000000ab/status",
            json={"status": "completed", "version": 1},
        )
        assert_eq(response.status_code, 404)


@test()
def set_todo_status_tool_reports_conflict_in_the_envelope() -> None:
    """The `set_todo_status` tool surfaces a stale version as an envelope conflict."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        todo = create_todo(client, "water plants")
        first = call_tool(
            client,
            "set_todo_status",
            todo_id=todo["id"],
            version=todo["version"],
            status="completed",
        )
        assert_true(first["success"])
        stale = call_tool(
            client,
            "set_todo_status",
            todo_id=todo["id"],
            version=todo["version"],
            status="abandoned",
        )
        assert_false(stale["success"])
        assert_eq(stale["error"]["code"], "conflict")


@test()
def create_todo_tool_rejects_a_blank_action() -> None:
    """A blank action is a well-formed `invalid_input` envelope, not a row."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        envelope = call_tool(client, "create_todo", action="   ")
        assert_false(envelope["success"])
        assert_eq(envelope["error"]["code"], "invalid_input")
