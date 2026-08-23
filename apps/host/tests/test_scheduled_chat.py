"""Scheduled agent prompts entering the canonical chat transcript."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid7

from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def make_client(root: Path) -> TestClient:
    """Create an app with a deterministic conversation model and fast scheduler."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                default_model_id="tether-chat-text-faux",
                default_model_provider="faux",
                extra_extension_paths=(
                    Path(__file__).resolve().parents[2]
                    / "agent/tests/fixtures/faux-chat-two-turns.ts",
                ),
                kb_root=root / ".tether",
                scheduler_tick_seconds=0.05,
                session_secret=SESSION_SECRET,
                tool_base_url="http://127.0.0.1:9",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the browser surface."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def wait_for_answer(
    client: TestClient, conversation_id: str, *, message_count: int = 2
) -> list[dict[str, Any]]:
    """Poll the public transcript briefly until the expected turn settles."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
        if len(messages) >= message_count:
            return messages
        time.sleep(0.05)
    raise AssertionError("scheduled chat turn did not settle")


def wait_for_occurrence(
    client: TestClient,
    trigger_id: str,
) -> dict[str, Any]:
    """Poll briefly until one prompt occurrence reaches durable success."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        trigger = next(
            item
            for item in client.get("/api/triggers").json()
            if item["id"] == trigger_id
        )
        occurrence = trigger["latest_occurrence"]
        if occurrence is not None and occurrence["status"] == "succeeded":
            return trigger
        time.sleep(0.05)
    raise AssertionError("scheduled occurrence did not settle")


def schedule_prompt(client: TestClient, prompt: str) -> dict[str, Any]:
    """Create one targeted prompt occurrence due on the fast scheduler."""
    target_id = client.get("/api/conversations").json()[0]["id"]
    created = client.post(
        "/api/triggers",
        json={
            "recurrence": "once",
            "action_kind": "prompt",
            "payload": prompt,
            "target_conversation_id": target_id,
            "fire_at": (datetime.now(UTC) + timedelta(milliseconds=200)).isoformat(),
        },
    )
    assert_eq(created.status_code, 201)
    return created.json()


@test()
def a_fired_agent_prompt_runs_as_a_scheduled_chat_turn() -> None:
    """The target transcript marks the automated prompt as Scheduled context."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        trigger = schedule_prompt(client, "summarise my day")
        messages = wait_for_answer(client, conversation_id)
        stored = wait_for_occurrence(client, trigger["id"])

    assert_eq(
        [(message["role"], message["content"]) for message in messages],
        [("scheduled", "summarise my day"), ("assistant", "script complete")],
    )
    assert_eq(stored["target_conversation_id"], conversation_id)
    assert_eq(stored["target_conversation_name"], "Main")
    assert_eq(stored["latest_occurrence"]["status"], "succeeded")
    assert_eq(stored["latest_occurrence"]["turn"]["status"], "succeeded")
    assert_eq(
        messages[0]["turn"]["occurrence_id"],
        stored["latest_occurrence"]["id"],
    )


@test()
def exact_occurrence_route_survives_later_recurrence_and_trigger_deletion() -> None:
    """A Scheduled Message keeps an immutable inspectable firing identity."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        trigger = schedule_prompt(client, "summarise my day")
        messages = wait_for_answer(client, conversation_id)
        stored = wait_for_occurrence(client, trigger["id"])
        occurrence_id = messages[0]["turn"]["occurrence_id"]
        deleted = client.delete(
            f"/api/triggers/{trigger['id']}",
            params={"version": stored["version"]},
        )
        if deleted.status_code == 409:
            current = next(
                item
                for item in client.get("/api/triggers").json()
                if item["id"] == trigger["id"]
            )
            deleted = client.delete(
                f"/api/triggers/{trigger['id']}",
                params={"version": current["version"]},
            )

        occurrence = client.get(f"/api/scheduled-occurrences/{occurrence_id}")

    assert_eq(deleted.status_code, 200)
    assert_eq(occurrence.status_code, 200)
    assert_eq(occurrence.json()["id"], occurrence_id)
    assert_eq(occurrence.json()["turn"]["id"], messages[0]["turn_id"])
    assert_eq(occurrence.json()["target_conversation_kind"], "main")


@test()
def a_scheduled_prompt_waits_for_an_active_chat_turn() -> None:
    """A prompt firing during generation enters the transcript after that turn."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "interactive prompt",
                }
            )
            while websocket.receive_json().get("event") != "user_message":
                pass
            schedule_prompt(client, "scheduled prompt")
            while websocket.receive_json().get("event") != "agent_end":
                pass

        messages = wait_for_answer(client, conversation_id, message_count=4)

    assert_eq(
        [(message["role"], message["content"]) for message in messages],
        [
            ("user", "interactive prompt"),
            ("assistant", "script complete"),
            ("scheduled", "scheduled prompt"),
            ("assistant", "script complete"),
        ],
    )


@test()
def a_fired_agent_prompt_invalidates_connected_chat_messages() -> None:
    """A connected chat is told to reload after the scheduled turn settles."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        _ = client.get("/api/conversations")
        with client.websocket_connect("/ws") as websocket:
            schedule_prompt(client, "summarise my day")
            for _attempt in range(50):
                frame = websocket.receive_json()
                if frame.get("type") == "invalidate" and "messages" in frame.get(
                    "keys", []
                ):
                    break
            else:
                raise AssertionError("scheduled chat turn did not invalidate messages")

    assert_eq(sorted(frame["keys"]), ["conversations", "messages"])


@test()
def a_fired_agent_prompt_does_not_create_an_inbox_notification() -> None:
    """Chat delivery replaces the separate fired-notification Inbox item."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        schedule_prompt(client, "summarise my day")
        _ = wait_for_answer(client, conversation_id)

        notifications = client.get("/api/notifications").json()

    assert_eq(notifications, [])
