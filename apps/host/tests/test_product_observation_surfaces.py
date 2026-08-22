"""Tool and HTTP behavior for Product observations."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from snektest import assert_eq, assert_in, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.surfaces import SESSION, call_tool, login, surface_client
from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft


def _begin_feedback_turn(client: TestClient, wording: str) -> UUID:
    """Persist the active user Message, open its run, and return its Conversation."""
    runtime = app_runtime(cast("Starlette", client.app))
    conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
    if client.portal is None:
        raise RuntimeError("test client portal is not running")
    _ = client.portal.call(
        runtime.conversation_service.append_message,
        MessageDraft(
            content=wording,
            conversation_id=conversation_id,
            role="user",
        ),
    )
    _ = runtime.trace_recorder.begin_run(
        session_id=SESSION,
        kind="conversation",
        conversation_id=str(conversation_id),
        prompt=wording,
    )
    return conversation_id


@test()
def record_product_observation_tool_uses_the_active_user_message() -> None:
    """Chat capture preserves server-owned wording and Message provenance."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        wording = "You should have reminded me about that workout."
        _begin_feedback_turn(client, wording)

        envelope = call_tool(
            client,
            "record_product_observation",
            interpretation="Resurface same-day exercise intentions.",
        )

        assert_true(envelope["success"])
        observation = envelope["result"]
        assert_eq(observation["wording"], wording)
        assert_eq(
            observation["interpretation"],
            "Resurface same-day exercise intentions.",
        )
        assert_in("conversation_id", observation)
        assert_in("message_id", observation)
        listing = client.get("/api/product-observations")
        assert_eq(listing.status_code, 200)
        assert_eq([item["id"] for item in listing.json()], [observation["id"]])


@test()
def record_product_observation_finds_the_user_message_after_another_tool() -> None:
    """Earlier tool use in the same turn does not hide the feedback source."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        wording = "Log the reminder timing as feedback."
        conversation_id = _begin_feedback_turn(client, wording)
        runtime = app_runtime(cast("Starlette", client.app))
        if client.portal is None:
            raise RuntimeError("test client portal is not running")
        _ = client.portal.call(
            runtime.conversation_service.append_message,
            MessageDraft(
                content="list_todos",
                conversation_id=conversation_id,
                role="tool",
                tool_name="list_todos",
                tool_result={"ready": [], "waiting": []},
            ),
        )

        envelope = call_tool(
            client,
            "record_product_observation",
            interpretation="Improve reminder timing.",
        )

        assert_true(envelope["success"])
        assert_eq(envelope["result"]["wording"], wording)


@test()
def list_product_observations_tool_returns_open_feedback() -> None:
    """Chat can inspect unresolved feedback without mutating it."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_feedback_turn(client, "The reminder was too late.")
        recorded = call_tool(
            client,
            "record_product_observation",
            interpretation="Let reminders account for preparation time.",
        )

        listing = call_tool(client, "list_product_observations")

        assert_true(listing["success"])
        assert_eq(
            [item["id"] for item in listing["result"]],
            [recorded["result"]["id"]],
        )


@test()
def resolve_product_observation_route_removes_feedback_from_the_open_list() -> None:
    """The browser resolves feedback at its observed version."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_feedback_turn(client, "The reminder was too late.")
        observation = call_tool(
            client,
            "record_product_observation",
            interpretation="Let reminders account for preparation time.",
        )["result"]

        response = client.post(
            f"/api/product-observations/{observation['id']}/resolve",
            json={"version": observation["version"]},
        )

        assert_eq(response.status_code, 200)
        assert_eq(response.json()["status"], "resolved")
        assert_eq(client.get("/api/product-observations").json(), [])
