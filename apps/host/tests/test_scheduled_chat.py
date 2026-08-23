"""Scheduled agent prompts entering the canonical chat transcript."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.app_runtime import app_runtime
from tether.chat_turn import ChatTurnDependencies
from tether.model_selection import AgentModelConfig
from tether.scheduler import ScheduledChatPromptRunner
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


def make_model_app(root: Path) -> FastAPI:
    """Create an app whose faux provider reports the model used for each turn."""
    return create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            default_model="cheap",
            extra_extension_paths=(
                Path(__file__).resolve().parents[2]
                / "agent/tests/fixtures/model-echo-faux.ts",
            ),
            kb_root=root / ".tether",
            model_allowlist=(
                AgentModelConfig(
                    display_name="Cheap",
                    id="cheap",
                    model_id="tether-chat-cheap-faux",
                    provider="faux",
                ),
                AgentModelConfig(
                    display_name="Smart",
                    id="smart",
                    model_id="tether-chat-smart-faux",
                    provider="faux",
                ),
            ),
            scheduler_tick_seconds=60,
            session_secret=SESSION_SECRET,
            tool_base_url="http://127.0.0.1:9",
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
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


def schedule_prompt(client: TestClient, prompt: str) -> None:
    """Create one agent-prompt trigger due on the fast scheduler."""
    created = client.post(
        "/api/triggers",
        json={
            "recurrence": "once",
            "action_kind": "prompt",
            "payload": prompt,
            "fire_at": (datetime.now(UTC) + timedelta(milliseconds=200)).isoformat(),
        },
    )
    assert_eq(created.status_code, 201)


@test()
def a_fired_agent_prompt_runs_as_a_user_chat_turn() -> None:
    """The normal chat transcript contains the scheduled prompt and its answer."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        schedule_prompt(client, "summarise my day")
        messages = wait_for_answer(client, conversation_id)

    assert_eq(
        [(message["role"], message["content"]) for message in messages],
        [("user", "summarise my day"), ("assistant", "script complete")],
    )


@test()
def a_scheduled_prompt_uses_its_pinned_profile() -> None:
    """The scheduled turn runs with its profile instead of the chat default."""
    with TemporaryDirectory() as directory:
        application = make_model_app(Path(directory))
        with TestClient(application) as client:
            login(client)
            runtime = app_runtime(application)
            runner = ScheduledChatPromptRunner(
                ChatTurnDependencies(
                    conversation_service=runtime.conversation_service,
                    dreaming_enabled=runtime.dreaming_enabled,
                    dreaming_service=runtime.dreaming_service,
                    logger=runtime.logger,
                    runtime_registry=runtime.conversation_runtime_registry,
                    trace_recorder=runtime.trace_recorder,
                    turn_queue=runtime.conversation_turn_queue,
                ),
                event_publisher=runtime.event_hub,
            )
            portal = client.portal
            if portal is None:
                raise AssertionError("test client portal is unavailable")

            answer = portal.call(runner.run, "summarise my day", "smart")

    assert_eq(answer, "tether-chat-smart-faux")


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
            ("user", "scheduled prompt"),
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

    assert_eq(frame["keys"], ["messages", "conversations"])


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
