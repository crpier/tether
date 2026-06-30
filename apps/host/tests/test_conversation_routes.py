"""REST behavior tests for host-owned conversations and transcript."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID

from snektest import assert_eq, assert_in, assert_len, test
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tether.conversations import MessageDraft
from tether.model_selection import AgentModelConfig
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import TOOL_AUTH_HEADER

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


class FakePiClient:
    """Prompt command test double."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def request(self, command_type: str, **fields: object) -> dict[str, object]:
        """Accept host-sent commands without starting a subprocess."""
        self.commands.append(command_type)
        self.requests.append((command_type, fields))
        return {"success": command_type in {"prompt", "abort", "set_model"}}


class FakeRuntime:
    """pi runtime test double with queued protocol events."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.client: FakePiClient = FakePiClient()
        self._events: list[dict[str, Any]] = events

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Return the next queued event."""
        _ = event_type
        _ = wait_seconds
        return self._events.pop(0)


class FailingPromptClient(FakePiClient):
    """Prompt client that returns pi's failure payload."""

    async def request(self, command_type: str, **fields: object) -> dict[str, object]:
        """Fail prompts with the configured provider error."""
        self.commands.append(command_type)
        self.requests.append((command_type, fields))
        if command_type == "prompt":
            return {"success": False, "error": "No API key for openai-codex/gpt-5.5"}
        return {"success": True}


class FailingPromptRuntime:
    """Runtime whose prompt command fails before streaming starts."""

    def __init__(self) -> None:
        self.client: FailingPromptClient = FailingPromptClient()

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Prompt failure should prevent stream consumption."""
        _ = event_type
        _ = wait_seconds
        message = "stream should not be read after prompt failure"
        raise AssertionError(message)


class BlockingRuntime:
    """Runtime whose generation waits until the test releases an event."""

    def __init__(self) -> None:
        self.client: FakePiClient = FakePiClient()
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Wait for the next queued event."""
        _ = event_type
        return await asyncio.wait_for(self.events.get(), timeout=wait_seconds)


class FakeRuntimeRegistry:
    """Conversation runtime registry test double."""

    def __init__(self, runtime: object) -> None:
        self.runtime: object = runtime

    def current_for(self, conversation_id: object) -> object:
        """Return the configured fake runtime without spawning."""
        _ = conversation_id
        return self.runtime

    async def runtime_for(self, conversation: object) -> object:
        """Return the configured fake runtime."""
        _ = conversation
        return self.runtime

    async def shutdown_all(self) -> None:
        """Match the production registry shutdown hook."""


class OrderedRuntime:
    """Runtime double that records drain/prompt ordering in one log."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.client: FakePiClient = FakePiClient()
        self._events: list[dict[str, Any]] = events

    def drain_events(self) -> int:
        """Log the per-prompt drain into the shared command log."""
        self.client.commands.append("drain")
        return 0

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Return the next queued event."""
        _ = event_type
        _ = wait_seconds
        return self._events.pop(0)


class TimeoutRuntime:
    """Runtime double whose generation never produces an event."""

    def __init__(self) -> None:
        self.client: FakePiClient = FakePiClient()

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Simulate pi going silent past the agent-event timeout."""
        _ = event_type
        _ = wait_seconds
        message = "agent event timed out"
        raise TimeoutError(message)


def make_client(root: Path) -> TestClient:
    """Create a test app with isolated persistent DB and `.tether` root."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def make_model_client(root: Path) -> TestClient:
    """Create a test app with a curated model allowlist."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                default_model="cheap",
                kb_root=root / ".tether",
                model_allowlist=(
                    AgentModelConfig(
                        display_name="Cheap Faux",
                        id="cheap",
                        model_id="tether-chat-cheap-faux",
                        provider="faux",
                    ),
                    AgentModelConfig(
                        display_name="Smart Faux",
                        id="smart",
                        model_id="tether-chat-smart-faux",
                        provider="faux",
                    ),
                ),
                session_secret=SESSION_SECRET,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def make_faux_chat_client(root: Path) -> TestClient:
    """Create a test app whose pi runtime uses the faux chat provider."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                default_model_id="tether-chat-text-faux",
                default_model_provider="faux",
                extra_extension_paths=(
                    Path(__file__).resolve().parents[2]
                    / "agent/tests/fixtures/faux-chat-text.ts",
                ),
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                tool_base_url="http://127.0.0.1:9",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def make_model_echo_client(root: Path) -> TestClient:
    """Create a test app whose faux provider echoes the active model id."""
    return TestClient(
        create_app(
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
                        display_name="Cheap Faux",
                        id="cheap",
                        model_id="tether-chat-cheap-faux",
                        provider="faux",
                    ),
                    AgentModelConfig(
                        display_name="Smart Faux",
                        id="smart",
                        model_id="tether-chat-smart-faux",
                        provider="faux",
                    ),
                ),
                session_secret=SESSION_SECRET,
                tool_base_url="http://127.0.0.1:9",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def prompt_until_agent_end(
    client: TestClient,
    *,
    conversation_id: str,
    content: str,
) -> None:
    """Send one browser prompt and wait for completion."""
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(
            {
                "type": "prompt",
                "conversation_id": conversation_id,
                "content": content,
            }
        )
        while websocket.receive_json().get("event") != "agent_end":
            pass


@test()
def models_route_returns_curated_allowlist() -> None:
    """`GET /api/models` exposes only host-configured models."""
    with (
        TemporaryDirectory() as directory,
        make_model_client(Path(directory)) as client,
    ):
        login(client)
        response = client.get("/api/models")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "default_model": "cheap",
            "models": [
                {
                    "display_name": "Cheap Faux",
                    "id": "cheap",
                    "model_id": "tether-chat-cheap-faux",
                    "provider": "faux",
                },
                {
                    "display_name": "Smart Faux",
                    "id": "smart",
                    "model_id": "tether-chat-smart-faux",
                    "provider": "faux",
                },
            ],
        },
    )


@test()
def conversations_route_creates_default_conversation() -> None:
    """`GET /api/conversations` exposes one durable default conversation."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.get("/api/conversations")

    assert_eq(response.status_code, 200)
    conversations = response.json()
    assert_len(conversations, 1)
    assert_eq(conversations[0]["title"], None)
    assert_eq(conversations[0]["selected_model"], None)


@test()
def configured_default_model_is_stored_on_new_conversations() -> None:
    """New conversation rows inherit the global default model id."""
    with (
        TemporaryDirectory() as directory,
        make_model_client(Path(directory)) as client,
    ):
        login(client)
        response = client.get("/api/conversations")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()[0]["selected_model"], "cheap")


@test()
def setting_model_persists_and_updates_live_runtime() -> None:
    """Changing the model stores the selection and dispatches `set_model`."""
    fake_runtime = FakeRuntime([])
    with (
        TemporaryDirectory() as directory,
        make_model_client(Path(directory)) as client,
    ):
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/model",
            json={"selected_model": "smart"},
        )
        stored = client.get("/api/conversations").json()[0]

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["selected_model"], "smart")
    assert_eq(stored["selected_model"], "smart")
    assert_eq(
        fake_runtime.client.requests,
        [
            (
                "set_model",
                {"provider": "faux", "modelId": "tether-chat-smart-faux"},
            )
        ],
    )


@test()
def messages_route_returns_empty_default_transcript() -> None:
    """`GET /api/conversations/{id}/messages` rehydrates settled history."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversations_response = client.get("/api/conversations")
        conversation_id = conversations_response.json()[0]["id"]

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), [])


@test()
def default_conversation_survives_app_restart() -> None:
    """The host stores conversations in the configured SQLite database."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with make_client(root) as client:
            login(client)
            conversation_id = client.get("/api/conversations").json()[0]["id"]

        with make_client(root) as client:
            login(client)
            response = client.get("/api/conversations")

    assert_eq(response.status_code, 200)
    assert_in(conversation_id, [conversation["id"] for conversation in response.json()])


@test()
def stored_model_is_reapplied_after_runtime_respawn() -> None:
    """A respawned pi process uses the conversation's persisted model."""
    with (
        TemporaryDirectory() as directory,
        make_model_echo_client(Path(directory)) as client,
    ):
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        prompt_until_agent_end(
            client,
            conversation_id=conversation_id,
            content="Use the default model",
        )
        set_response = client.post(
            f"/api/conversations/{conversation_id}/model",
            json={"selected_model": "smart"},
        )
        portal = client.portal
        assert portal is not None
        portal.call(
            cast(
                "Starlette", client.app
            ).state.conversation_runtime_registry.shutdown_all
        )

        prompt_until_agent_end(
            client,
            conversation_id=conversation_id,
            content="Use the persisted model",
        )
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert_eq(set_response.status_code, 200)
    assert_eq(set_response.json()["selected_model"], "smart")
    assert_eq(
        [message["content"] for message in messages if message["role"] == "assistant"],
        ["tether-chat-cheap-faux", "tether-chat-smart-faux"],
    )


@test()
def websocket_rejects_unauthenticated_handshake() -> None:
    """`/ws` requires the signed browser session cookie on upgrade."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        try:
            with client.websocket_connect("/ws"):
                close_code = 1000
        except WebSocketDisconnect as error:
            close_code = error.code

    assert_eq(close_code, 1008)


@test()
def websocket_prompt_persists_user_message() -> None:
    """Inbound `prompt` stores the user row before generation starts."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Hello from ws",
                }
            )
            _ = websocket.receive_json()

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()[0]["role"], "user")
    assert_eq(response.json()[0]["content"], "Hello from ws")
    assert_eq(response.json()[0]["seq"], 1)


@test()
def websocket_prompt_streams_and_persists_assistant_message() -> None:
    """A pi-backed prompt streams completion and stores the settled assistant row."""
    with (
        TemporaryDirectory() as directory,
        make_faux_chat_client(Path(directory)) as client,
    ):
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Hello from pi",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq([message["role"] for message in messages], ["user", "assistant"])
    assert_eq(messages[1]["content"], "script complete")
    assert_eq(messages[1]["seq"], 2)


@test()
def websocket_prompt_failure_reports_pi_detail() -> None:
    """A failed pi prompt surfaces the provider-specific detail to the browser."""
    runtime = FailingPromptRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            _ = websocket.receive_json()
            frame = websocket.receive_json()

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "prompt failed: No API key for openai-codex/gpt-5.5")


@test()
def websocket_persists_assistant_message_from_streamed_deltas() -> None:
    """The host assembles streamed text when pi's final event has no content."""
    fake_runtime = FakeRuntime(
        [
            {"type": "message_start", "message": {"role": "assistant"}},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": {"text": "streamed "},
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": {"text": "answer"},
                },
            },
            {"type": "message_end", "message": {"role": "assistant"}},
            {"type": "agent_end"},
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Stream please",
                }
            )
            frames: list[dict[str, object]] = []
            while True:
                frame = cast("dict[str, object]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq(
        [frame.get("event") for frame in frames],
        [
            "user_message",
            "message_start",
            "text_delta",
            "text_delta",
            "message_end",
            "agent_end",
        ],
    )
    assert_eq(messages[1]["content"], "streamed answer")


@test()
def websocket_drains_stale_events_before_prompt() -> None:
    """Each prompt drains leftover events before driving pi (queue hygiene)."""
    runtime = OrderedRuntime([{"type": "agent_end"}])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

    assert_eq(runtime.client.commands[:2], ["drain", "prompt"])


@test()
def websocket_reports_agent_timeout_to_browser() -> None:
    """A silent pi past the agent-event timeout surfaces an error frame."""
    runtime = TimeoutRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            _ = websocket.receive_json()
            frame = websocket.receive_json()

    assert_eq(frame["event"], "error")
    assert_in("timed out", frame["detail"])


@test()
def websocket_persists_reasoning_as_its_own_row_before_the_answer() -> None:
    """Thinking deltas settle into a reasoning row, never merged into the answer."""
    fake_runtime = FakeRuntime(
        [
            {"type": "message_start", "message": {"role": "assistant"}},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_delta",
                    "delta": {"text": "secret reasoning"},
                    "contentIndex": 0,
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": {"text": "answer"},
                    "contentIndex": 1,
                },
            },
            {"type": "message_end", "message": {"role": "assistant"}},
            {"type": "agent_end"},
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Think then answer",
                }
            )
            frames: list[dict[str, object]] = []
            while True:
                frame = cast("dict[str, object]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq(
        [(message["role"], message["content"]) for message in messages],
        [
            ("user", "Think then answer"),
            ("reasoning", "secret reasoning"),
            ("assistant", "answer"),
        ],
    )
    forwarded = [frame.get("event") for frame in frames]
    assert_in("thinking_delta", forwarded)
    assert_in("text_delta", forwarded)


@test()
def append_message_is_idempotent_for_pi_message_ids() -> None:
    """Retries for a pi message id do not duplicate transcript rows."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        service = cast("Starlette", client.app).state.conversation_service
        portal = client.portal
        assert portal is not None

        first = portal.call(
            service.append_message,
            MessageDraft(
                content="capture",
                conversation_id=UUID(conversation_id),
                pi_message_id="call-capture",
                role="tool",
                tool_name="capture",
                tool_result={"ok": True},
            ),
        )
        second = portal.call(
            service.append_message,
            MessageDraft(
                content="capture again",
                conversation_id=UUID(conversation_id),
                pi_message_id="call-capture",
                role="tool",
                tool_name="capture",
                tool_result={"ok": True},
            ),
        )

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(first.id, second.id)
    assert_len(response.json(), 1)


@test()
def websocket_invalidation_frames_reach_connected_clients() -> None:
    """Service-layer Memory writes publish invalidate frames over `/ws`."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        with client.websocket_connect("/ws") as websocket:
            response = client.post("/api/memories", json={"content": "notify me"})
            frame = websocket.receive_json()

    assert_eq(response.status_code, 201)
    assert_eq(frame, {"type": "invalidate", "keys": ["memories", "review-queue"]})


@test()
def websocket_internal_tool_capture_publishes_invalidation() -> None:
    """Agent tool calls mutate services and fan out invalidation frames."""
    session_id = "019f0906-0000-7000-8000-000000000001"
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        cast("Starlette", client.app).state.session_registry.register(session_id)
        tool_secret = cast("str", cast("Starlette", client.app).state.tool_secret)
        with client.websocket_connect("/ws") as websocket:
            response = client.post(
                "/internal/tools/capture",
                headers={TOOL_AUTH_HEADER: tool_secret},
                json={"session_id": session_id, "content": "tool memory"},
            )
            frame = websocket.receive_json()

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["success"], True)
    assert_eq(frame, {"type": "invalidate", "keys": ["memories", "review-queue"]})


@test()
def websocket_bucket_write_publishes_invalidation() -> None:
    """Bucket mutations publish their cache key through the shared WS hub."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        with client.websocket_connect("/ws") as websocket:
            response = client.post(
                "/api/bucket-items",
                json={
                    "item_type": "movie",
                    "data": {"title": "Dune"},
                    "intent_context": "recommended",
                },
            )
            frame = websocket.receive_json()

    assert_eq(response.status_code, 201)
    assert_eq(frame, {"type": "invalidate", "keys": ["bucket-items"]})


@test()
def websocket_abort_forwards_to_current_runtime() -> None:
    """Inbound `abort` asks the current pi runtime to stop generation."""
    fake_runtime = FakeRuntime([])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "abort", "conversation_id": conversation_id})
            frame = websocket.receive_json()

    assert_eq(frame["event"], "abort_ack")
    assert_eq(fake_runtime.client.commands, ["abort"])


@test()
def websocket_abort_is_processed_while_generation_is_running() -> None:
    """The receive loop stays alive while a prompt stream is in flight."""
    fake_runtime = BlockingRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Wait for abort",
                }
            )
            first_frame = websocket.receive_json()
            websocket.send_json({"type": "abort", "conversation_id": conversation_id})
            abort_frame = websocket.receive_json()

    assert_eq(first_frame["event"], "user_message")
    assert_eq(abort_frame["event"], "abort_ack")
    assert_eq(fake_runtime.client.commands, ["prompt", "abort"])


@test()
def websocket_persists_tool_call_rows() -> None:
    """Tool completion events settle as compact transcript rows."""
    fake_runtime = FakeRuntime(
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "call-capture",
                "toolName": "capture",
                "args": {"content": "tool memory"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-capture",
                "toolName": "capture",
                "result": {"details": {"result": {"id": "memory-id"}}},
                "isError": False,
            },
            {"type": "agent_end"},
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        cast(
            "Starlette", client.app
        ).state.conversation_runtime_registry = FakeRuntimeRegistry(fake_runtime)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "Use a tool",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq([message["role"] for message in messages], ["user", "tool"])
    assert_eq(messages[1]["tool_name"], "capture")
    assert_eq(messages[1]["tool_args"], {"content": "tool memory"})
    assert_eq(messages[1]["tool_result"], {"details": {"result": {"id": "memory-id"}}})
    assert_eq(messages[1]["pi_message_id"], "call-capture")
