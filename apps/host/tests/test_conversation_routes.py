"""REST behavior tests for host-owned conversations and transcript."""

import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID, uuid7

from snekql.sqlite import insert, update
from snektest import (
    assert_eq,
    assert_in,
    assert_isinstance,
    assert_len,
    assert_not_in,
    assert_true,
    test,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tether import server
from tether.app_runtime import app_runtime
from tether.chat_prompt import local_timezone_name, prompt_with_time_context
from tether.conversation_model import MessageDraft
from tether.conversation_store import ConversationTurn, Message
from tether.conversations import ConversationService
from tether.model_selection import AgentModelConfig
from tether.pi_runtime import ContextUsage
from tether.pi_turn_events import (
    AgentEnded,
    AssistantStreamNote,
    MessageSettled,
    ModelTurnStarted,
    TextDelta,
    ThinkingDelta,
    ToolSettled,
    ToolStarted,
    TurnEvent,
)
from tether.search_projection.embeddings import FakeEmbedder
from tether.server import AppConfig, HostSettings, create_app
from tether.telemetry import TelemetrySettings

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
    """pi runtime test double that streams queued typed turn events."""

    def __init__(
        self,
        turn_events: list[TurnEvent],
        *,
        loaded_skills: tuple[str, ...] = (),
        skills_confirmed: bool = False,
        context_usage: ContextUsage | None = None,
    ) -> None:
        self.client: FakePiClient = FakePiClient()
        self.loaded_skills: tuple[str, ...] = loaded_skills
        self.skills_confirmed: bool = skills_confirmed
        self._context_usage: ContextUsage | None = context_usage
        self._turn_events: list[TurnEvent] = turn_events

    async def fetch_context_usage(self) -> ContextUsage | None:
        """Return the configured pi context estimate."""
        return self._context_usage

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def shutdown(self) -> None:
        """Match the production runtime's teardown hook."""

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Yield the queued typed events of one turn."""
        _ = wait_seconds
        for turn_event in self._turn_events:
            yield turn_event


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

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Prompt failure should prevent stream consumption."""
        _ = wait_seconds
        message = "stream should not be read after prompt failure"
        raise AssertionError(message)
        # Unreachable by design: the yield makes this an async generator so
        # iteration (not the call) raises, matching the production runtime.
        yield AgentEnded()


class BlockingRuntime:
    """Runtime whose generation waits until the test releases an event."""

    def __init__(self) -> None:
        self.client: FakePiClient = FakePiClient()
        self.events: asyncio.Queue[TurnEvent] = asyncio.Queue()

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Yield each event as the test releases it."""
        while True:
            turn_event = await asyncio.wait_for(self.events.get(), timeout=wait_seconds)
            yield turn_event
            if isinstance(turn_event, AgentEnded):
                return


class FakeRuntimeRegistry:
    """Conversation runtime registry test double."""

    def __init__(self, runtime: object) -> None:
        self.runtime: object = runtime
        self.applied_models: list[tuple[object, AgentModelConfig]] = []
        self.discarded: list[object] = []

    def current_for(self, conversation_id: object) -> object:
        """Return the configured fake runtime without spawning."""
        _ = conversation_id
        return self.runtime

    async def runtime_for(self, conversation: object) -> object:
        """Return the configured fake runtime."""
        _ = conversation
        return self.runtime

    async def set_model(self, conversation_id: object, model: AgentModelConfig) -> None:
        """Record the model applied to a conversation's live runtime."""
        self.applied_models.append((conversation_id, model))

    async def discard(self, conversation_id: object) -> None:
        """Record the conversation whose runtime was torn down."""
        self.discarded.append(conversation_id)

    async def shutdown_all(self) -> None:
        """Match the production registry shutdown hook."""


class OrderedRuntime:
    """Runtime double that records drain/prompt ordering in one log."""

    def __init__(self, turn_events: list[TurnEvent]) -> None:
        self.client: FakePiClient = FakePiClient()
        self._turn_events: list[TurnEvent] = turn_events

    def drain_events(self) -> int:
        """Log the per-prompt drain into the shared command log."""
        self.client.commands.append("drain")
        return 0

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Yield the queued typed events of one turn."""
        _ = wait_seconds
        for turn_event in self._turn_events:
            yield turn_event


class TimeoutRuntime:
    """Runtime double whose generation never produces an event."""

    def __init__(self) -> None:
        self.client: FakePiClient = FakePiClient()

    def drain_events(self) -> int:
        """Match the production runtime's per-prompt queue hygiene hook."""
        return 0

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Simulate pi going silent past the agent-event timeout."""
        _ = wait_seconds
        message = "agent event timed out"
        raise TimeoutError(message)
        # Unreachable by design: the yield makes this an async generator so
        # iteration (not the call) raises, matching the production runtime.
        yield AgentEnded()


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


def _set_runtime_registry(client: TestClient, registry: object) -> None:
    """Replace the live process registry for controlled WebSocket tests."""
    runtime = app_runtime(cast("Starlette", client.app))
    object.__setattr__(runtime, "conversation_runtime_registry", registry)
    object.__setattr__(
        runtime.conversation_turns.dependencies,
        "runtime_registry",
        registry,
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
                        thinking_level="medium",
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


def receive_event(websocket: Any, event: str) -> dict[str, Any]:
    """Receive frames through the requested chat event."""
    while True:
        frame = cast("dict[str, Any]", websocket.receive_json())
        if frame.get("event") == event:
            return frame


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
                "request_id": str(uuid7()),
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
                    "thinking_level": None,
                },
                {
                    "display_name": "Smart Faux",
                    "id": "smart",
                    "model_id": "tether-chat-smart-faux",
                    "provider": "faux",
                    "thinking_level": "medium",
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
    assert_eq(conversations[0]["kind"], "main")
    assert_eq(conversations[0]["status"], "active")
    assert_eq(conversations[0]["display_name"], None)
    assert_eq(conversations[0]["scope_brief"], None)
    assert_eq(conversations[0]["scope_revision"], 1)
    assert_eq(conversations[0]["last_read_seq"], 0)
    assert_eq(conversations[0]["latest_message_seq"], 0)
    assert_eq(conversations[0]["pending_turn_count"], 0)
    assert_eq(conversations[0]["running_turn_id"], None)
    assert_eq(conversations[0]["has_unread"], False)
    assert_eq(conversations[0]["archived_at"], None)


@test()
def authenticated_user_can_create_a_scoped_conversation() -> None:
    """`POST /api/conversations` creates a named active Scoped Conversation."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)

        response = client.post(
            "/api/conversations",
            json={
                "display_name": "Garden planning",
                "scope_brief": "Plan this year's vegetable garden.",
            },
        )

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["kind"], "scoped")
    assert_eq(response.json()["display_name"], "Garden planning")
    assert_eq(
        response.json()["scope_brief"],
        "Plan this year's vegetable garden.",
    )


@test()
def authenticated_user_can_fetch_one_conversation() -> None:
    """`GET /api/conversations/{id}` returns archived or active lifecycle state."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/conversations",
            json={
                "display_name": "Garden planning",
                "scope_brief": "Plan this year's vegetable garden.",
            },
        ).json()

        response = client.get(f"/api/conversations/{created['id']}")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["id"], created["id"])
    assert_eq(response.json()["display_name"], "Garden planning")


@test()
def authenticated_user_can_update_scoped_conversation() -> None:
    """`PATCH /api/conversations/{id}` edits scope through its durable revision."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/conversations",
            json={
                "display_name": "Garden planning",
                "scope_brief": "Plan this year's vegetable garden.",
            },
        ).json()

        response = client.patch(
            f"/api/conversations/{created['id']}",
            json={"scope_brief": "Plan vegetables and irrigation."},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["scope_brief"], "Plan vegetables and irrigation.")
    assert_eq(response.json()["scope_revision"], 2)


@test()
def rename_only_and_no_op_updates_preserve_scope_revision() -> None:
    """Only an actual scope-brief change rotates submitted scope state."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/conversations",
            json={"display_name": "Garden", "scope_brief": "Plan vegetables."},
        ).json()

        renamed = client.patch(
            f"/api/conversations/{created['id']}",
            json={"display_name": "Back garden"},
        ).json()
        unchanged = client.patch(
            f"/api/conversations/{created['id']}",
            json={"scope_brief": "Plan vegetables."},
        ).json()

    assert_eq(renamed["scope_revision"], 1)
    assert_eq(unchanged["scope_revision"], 1)


@test()
def authenticated_user_can_archive_a_scoped_conversation() -> None:
    """Archival discards the warm runtime and hides the Conversation by default."""
    registry = FakeRuntimeRegistry(FakeRuntime([]))
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, registry)
        login(client)
        created = client.post(
            "/api/conversations",
            json={
                "display_name": "Garden planning",
                "scope_brief": "Plan this year's vegetable garden.",
            },
        ).json()

        response = client.post(f"/api/conversations/{created['id']}/archive")
        ordinary = client.get("/api/conversations").json()
        with_archived = client.get(
            "/api/conversations", params={"include_archived": "true"}
        ).json()

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["status"], "archived")
    assert_not_in(created["id"], [item["id"] for item in ordinary])
    assert_in(created["id"], [item["id"] for item in with_archived])
    assert_eq([str(item) for item in registry.discarded], [created["id"]])


@test()
def authenticated_user_can_restore_an_archived_conversation() -> None:
    """`POST /restore` returns an archived Scoped Conversation to active state."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/conversations",
            json={
                "display_name": "Garden planning",
                "scope_brief": "Plan this year's vegetable garden.",
            },
        ).json()
        _ = client.post(f"/api/conversations/{created['id']}/archive")

        response = client.post(f"/api/conversations/{created['id']}/restore")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["status"], "active")
    assert_eq(response.json()["archived_at"], None)


@test()
def authenticated_user_can_mark_a_conversation_read() -> None:
    """`POST /read` advances durable read position to the current transcript tail."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service
        for content in ("seen", "raced"):
            _ = portal.call(
                service.append_message,
                MessageDraft(
                    content=content,
                    conversation_id=UUID(conversation_id),
                    role="assistant",
                ),
            )

        response = client.post(
            f"/api/conversations/{conversation_id}/read",
            json={"last_read_seq": 1},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["last_read_seq"], 1)


@test()
def conversations_route_derives_unread_and_working_state() -> None:
    """Conversation read models derive navigation state from durable rows."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service
        _ = portal.call(
            service.append_message,
            MessageDraft(
                content="unread answer",
                conversation_id=UUID(conversation_id),
                role="assistant",
            ),
        )

        async def add_work() -> UUID:
            async with service.database.transaction(mode="immediate") as transaction:
                running = await transaction.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=UUID(conversation_id),
                            origin="interactive",
                            scope_revision_snapshot=1,
                            status="running",
                            turn_seq=1,
                        )
                    ).returning()
                )
                _ = await transaction.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=UUID(conversation_id),
                            origin="interactive",
                            scope_revision_snapshot=1,
                            status="pending",
                            turn_seq=2,
                        )
                    )
                )
                return running.id

        running_id = portal.call(add_work)
        response = client.get("/api/conversations").json()[0]

    assert_eq(response["latest_message_seq"], 1)
    assert_eq(response["has_unread"], True)
    assert_eq(response["pending_turn_count"], 1)
    assert_eq(response["running_turn_id"], str(running_id))


@test()
def conversations_route_exposes_session_freshness_fields() -> None:
    """`ConversationRead` carries the gap and last-activity signal, not a hardcode."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation = client.get("/api/conversations").json()[0]

    assert_eq(conversation["session_gap_seconds"], 300)
    assert_eq(conversation["latest_activity"], None)


@test()
def latest_activity_reflects_the_most_recent_turn() -> None:
    """After a user row lands, `latest_activity` reports its timestamp."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "hello",
                }
            )
            _ = receive_event(websocket, "user_message")

        conversation = client.get("/api/conversations").json()[0]

    assert_true(conversation["latest_activity"] is not None)


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
def setting_model_persists_without_touching_the_live_runtime() -> None:
    """Model selection is durable profile state applied only on later execution."""
    registry = FakeRuntimeRegistry(FakeRuntime([]))
    with (
        TemporaryDirectory() as directory,
        make_model_client(Path(directory)) as client,
    ):
        _set_runtime_registry(client, registry)
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/model",
            json={"selected_model": "smart"},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["selected_model"], "smart")
    assert_eq(registry.applied_models, [])


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
def authenticated_user_can_list_nonterminal_conversation_turns() -> None:
    """`GET /turns` returns queued controls without transcript Messages."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service
        request_id = uuid7()

        async def add_pending_turn() -> UUID:
            async with service.database.transaction(mode="immediate") as transaction:
                pending = await transaction.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=UUID(conversation_id),
                            origin="interactive",
                            prompt_snapshot="queued after refresh",
                            reply_mode="spoken",
                            request_id=request_id,
                            scope_revision_snapshot=1,
                            status="pending",
                            turn_seq=1,
                        )
                    ).returning()
                )
                _ = await transaction.execute(
                    insert(
                        ConversationTurn(
                            completed_at=datetime.now(UTC),
                            conversation_id=UUID(conversation_id),
                            origin="interactive",
                            prompt_snapshot="already done",
                            reply_mode="text",
                            scope_revision_snapshot=1,
                            status="succeeded",
                            turn_seq=2,
                        )
                    )
                )
                return pending.id

        turn_id = portal.call(add_pending_turn)
        response = client.get(f"/api/conversations/{conversation_id}/turns")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        [
            {
                "completed_at": None,
                "conversation_id": conversation_id,
                "created_at": response.json()[0]["created_at"],
                "failure_code": None,
                "failure_summary": None,
                "id": str(turn_id),
                "origin": "interactive",
                "prompt": "queued after refresh",
                "reply_mode": "spoken",
                "request_id": str(request_id),
                "started_at": None,
                "status": "pending",
            }
        ],
    )


@test()
def authenticated_user_can_fetch_a_message_free_terminal_turn() -> None:
    """`GET /turns/{id}` exposes prompt and lifecycle without Messages."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service

        async def add_cancelled_turn() -> UUID:
            async with service.database.transaction(mode="immediate") as transaction:
                turn = await transaction.execute(
                    insert(
                        ConversationTurn(
                            completed_at=datetime.now(UTC),
                            conversation_id=UUID(conversation_id),
                            origin="scheduled",
                            prompt_snapshot="cancelled before execution",
                            reply_mode="text",
                            scope_revision_snapshot=1,
                            status="cancelled",
                            turn_seq=1,
                        )
                    ).returning()
                )
                return turn.id

        turn_id = portal.call(add_cancelled_turn)
        response = client.get(f"/api/conversations/{conversation_id}/turns/{turn_id}")
        messages = client.get(
            f"/api/conversations/{conversation_id}/messages",
            params={"turn_id": str(turn_id)},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["prompt"], "cancelled before execution")
    assert_eq(response.json()["status"], "cancelled")
    assert_eq(response.json()["origin"], "scheduled")
    assert_eq(messages.json(), [])


@test()
def conversation_turn_detail_is_scoped_to_its_conversation() -> None:
    """A turn UUID cannot be read through another Conversation route."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        main_id = client.get("/api/conversations").json()[0]["id"]
        scoped_id = client.post(
            "/api/conversations",
            json={"display_name": "Other", "scope_brief": "Other work."},
        ).json()["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service

        async def add_turn() -> UUID:
            async with service.database.transaction(mode="immediate") as transaction:
                turn = await transaction.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=UUID(main_id),
                            origin="interactive",
                            prompt_snapshot="private to Main",
                            reply_mode="text",
                            scope_revision_snapshot=1,
                            status="pending",
                            turn_seq=1,
                        )
                    ).returning()
                )
                return turn.id

        turn_id = portal.call(add_turn)
        response = client.get(f"/api/conversations/{scoped_id}/turns/{turn_id}")

    assert_eq(response.status_code, 404)


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
            app_runtime(
                cast("Starlette", client.app)
            ).conversation_runtime_registry.shutdown_all
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
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello from ws",
                }
            )
            _ = receive_event(websocket, "user_message")

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()[0]["role"], "user")
    assert_eq(response.json()[0]["content"], "Hello from ws")
    assert_eq(response.json()[0]["seq"], 1)


@test()
def prompt_time_context_carries_clock_and_zone() -> None:
    """The preamble stamps an ISO time + zone and keeps the user's text intact."""
    now = datetime(2026, 7, 1, 18, 23, 5, tzinfo=UTC)
    augmented = prompt_with_time_context(
        "remind me in 3 minutes", now=now, timezone_name="America/New_York"
    )

    assert_in("2026-07-01T18:23:05+00:00", augmented)
    assert_in("America/New_York", augmented)
    assert_true(augmented.endswith("remind me in 3 minutes"))


@test()
def local_timezone_name_prefers_tz_env() -> None:
    """An exported `TZ` wins over the /etc/localtime probe."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Bucharest"
    try:
        name = local_timezone_name(datetime(2026, 7, 1, tzinfo=UTC))
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous

    assert_eq(name, "Europe/Bucharest")


@test()
def websocket_prompt_sends_time_context_to_pi_not_history() -> None:
    """pi receives the clock preamble; the stored user row stays clean."""
    fake_runtime = FakeRuntime([AgentEnded()])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "remind me in 3 minutes",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    prompt_fields = [
        fields
        for command, fields in fake_runtime.client.requests
        if command == "prompt"
    ]
    assert_len(prompt_fields, 1)
    pi_message = cast("str", prompt_fields[0]["message"])
    assert_in("Tether note", pi_message)
    assert_true(pi_message.endswith("remind me in 3 minutes"))
    assert_eq(response.json()[0]["content"], "remind me in 3 minutes")


@test()
def websocket_prompt_with_spoken_mode_sends_guidance_and_keeps_history_clean() -> None:
    """A spoken prompt guides pi privately; the stored rows stay clean."""
    fake_runtime = FakeRuntime(
        [
            TextDelta(
                content_index=0,
                raw_delta="Tether is a local agent.",
                text="Tether is a local agent.",
            ),
            MessageSettled(reasoning="", text="Tether is a local agent."),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        agent_end: dict[str, object] = {}
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "what is tether",
                    "reply_mode": "spoken",
                }
            )
            while True:
                frame = websocket.receive_json()
                if frame.get("event") == "agent_end":
                    agent_end = frame
                    break

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    prompt_fields = [
        fields
        for command, fields in fake_runtime.client.requests
        if command == "prompt"
    ]
    assert_len(prompt_fields, 1)
    pi_message = cast("str", prompt_fields[0]["message"])
    assert_in("text-to-speech", pi_message)
    assert_in("Do not mention this instruction", pi_message)
    assert_true(pi_message.endswith("what is tether"))
    assert_eq(
        [message["content"] for message in response.json()],
        ["what is tether", "Tether is a local agent."],
    )
    turn_id = assert_isinstance(agent_end.pop("turn_id", None), str)
    _ = UUID(turn_id)
    assert_eq(
        agent_end,
        {
            "type": "chat",
            "conversation_id": conversation_id,
            "event": "agent_end",
            "reply_mode": "spoken",
            "final_text": "Tether is a local agent.",
            "tool_only": False,
        },
    )


@test()
def websocket_prompt_defaults_reply_mode_to_text() -> None:
    """An omitted reply_mode stays text: no spoken guidance reaches pi."""
    fake_runtime = FakeRuntime([AgentEnded()])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        agent_end: dict[str, object] = {}
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "plain question",
                }
            )
            while True:
                frame = websocket.receive_json()
                if frame.get("event") == "agent_end":
                    agent_end = frame
                    break

    prompt_fields = [
        fields
        for command, fields in fake_runtime.client.requests
        if command == "prompt"
    ]
    assert_len(prompt_fields, 1)
    pi_message = cast("str", prompt_fields[0]["message"])
    assert_not_in("text-to-speech", pi_message)
    turn_id = assert_isinstance(agent_end.pop("turn_id", None), str)
    _ = UUID(turn_id)
    assert_eq(
        agent_end,
        {
            "type": "chat",
            "conversation_id": conversation_id,
            "event": "agent_end",
            "reply_mode": "text",
            "final_text": "",
            "tool_only": False,
        },
    )


@test()
def websocket_prompt_rejects_unknown_reply_mode() -> None:
    """A reply_mode outside text/spoken fails validation before any turn."""
    fake_runtime = FakeRuntime([AgentEnded()])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "hello",
                    "reply_mode": "whisper",
                }
            )
            frame = websocket.receive_json()

    assert_eq(fake_runtime.client.commands, [])
    assert_eq(frame["type"], "chat")
    assert_eq(frame["event"], "error")


@test()
def prompt_time_context_scopes_spoken_guidance_to_the_final_answer() -> None:
    """Spoken guidance governs the final answer and forbids mentioning it."""
    now = datetime(2026, 7, 1, 18, 23, 5, tzinfo=UTC)
    spoken = prompt_with_time_context(
        "compare x and y",
        now=now,
        timezone_name="UTC",
        reply_mode="spoken",
    )

    assert_in("consumed through text-to-speech", spoken)
    assert_in("concise spoken summary", spoken)
    assert_in("Preserve normal reasoning and tool use", spoken)
    assert_in("Do not mention this instruction or the reply mode", spoken)
    assert_true(spoken.endswith("compare x and y"))


@test()
def prompt_time_context_makes_spoken_measurements_selective_and_rounded() -> None:
    """Spoken guidance turns metric-heavy source material into a listenable summary."""
    spoken = prompt_with_time_context(
        "How was my nap?",
        now=datetime(2026, 7, 1, 18, 23, 5, tzinfo=UTC),
        timezone_name="UTC",
        reply_mode="spoken",
    )

    assert_in(
        "summarize the pattern instead of reciting every available metric", spoken
    )
    assert_in("round them to listener-friendly precision", spoken)
    assert_in("Group or omit secondary figures", spoken)
    assert_in("Default to one or two key figures", spoken)
    assert_in("Hard limit: use at most two numeric quantities", spoken)
    assert_in(
        "Times, durations, percentages, measurements, and comparisons all count",
        spoken,
    )
    assert_in("silently count the numeric quantities", spoken)
    assert_in("rewrite it before responding", spoken)
    assert_in("express remaining detail qualitatively or omit it", spoken)
    assert_in("Do not give both a duration and its start and end times", spoken)
    assert_in("Keep secondary breakdown metrics out", spoken)
    assert_in("Give exact values when the user asks for them", spoken)


@test()
def prompt_time_context_omits_spoken_guidance_for_text_mode() -> None:
    """Text-mode prompts carry only the wall-clock note."""
    now = datetime(2026, 7, 1, 18, 23, 5, tzinfo=UTC)
    text = prompt_with_time_context(
        "compare x and y",
        now=now,
        timezone_name="UTC",
        reply_mode="text",
    )

    assert_not_in("text-to-speech", text)
    assert_true(text.endswith("compare x and y"))


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
                    "request_id": str(uuid7()),
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


async def _backdate_transcript(
    service: ConversationService, conversation_id: UUID, minutes: int
) -> None:
    """Age every transcript row so the next prompt reads as a cold gap."""
    stale = (datetime.now(UTC) - timedelta(minutes=minutes)).replace(tzinfo=None)
    async with service.database.transaction() as tx:
        _ = await tx.execute(
            update(Message)
            .set(Message.created_at.to(stale))
            .where(Message.conversation_id.eq(conversation_id))
        )


@test()
def local_dependency_profile_returns_deterministic_chat_without_credentials() -> None:
    """The composed local host drives a real pi turn through the Faux provider."""
    with TemporaryDirectory() as directory:
        local_root = Path(directory) / "local"
        config = server._app_config_from_settings(
            HostSettings(
                app_password=APP_PASSWORD,
                dependency_profile="local",
                local_data_root=local_root,
                session_secret=SESSION_SECRET,
                stt_api_key="production-key-is-ignored",
                tts_api_key="production-key-is-ignored",
            )
        )
        with TestClient(
            create_app(
                config=config,
                embedder=FakeEmbedder(),
                telemetry_settings=TelemetrySettings(install_global_provider=False),
            )
        ) as client:
            login(client)
            conversation_id = client.get("/api/conversations").json()[0]["id"]

            for content in ("Hello from local development", "Second local turn"):
                with client.websocket_connect("/ws") as websocket:
                    websocket.send_json(
                        {
                            "type": "prompt",
                            "request_id": str(uuid7()),
                            "conversation_id": conversation_id,
                            "content": content,
                        }
                    )
                    while websocket.receive_json().get("event") != "agent_end":
                        pass

            response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(
        [message["content"] for message in response.json()],
        [
            "Hello from local development",
            "Local development response.",
            "Second local turn",
            "Local development response.",
        ],
    )


@test()
def websocket_prompt_rotates_pi_session_after_a_cold_gap() -> None:
    """The server rotates the pi session when a prompt lands past the gap."""
    with (
        TemporaryDirectory() as directory,
        make_faux_chat_client(Path(directory)) as client,
    ):
        login(client)
        conversation = client.get("/api/conversations").json()[0]
        conversation_id = conversation["id"]
        before = conversation["pi_session_id"]
        prompt_until_agent_end(
            client, conversation_id=conversation_id, content="first topic"
        )
        warm = client.get("/api/conversations").json()[0]["pi_session_id"]

        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service
        portal.call(_backdate_transcript, service, UUID(conversation_id), 10)
        prompt_until_agent_end(
            client, conversation_id=conversation_id, content="new topic"
        )
        after = client.get("/api/conversations").json()[0]["pi_session_id"]
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert_eq(warm, before)
    assert_true(after != before)
    assert_eq(
        [message["role"] for message in messages],
        ["user", "assistant", "user", "assistant"],
    )


@test()
def websocket_prompt_failure_reports_stable_detail() -> None:
    """A failed pi prompt keeps raw provider diagnostics out of the browser."""
    runtime = FailingPromptRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            frame = receive_event(websocket, "error")

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "Agent could not accept the prompt.")


@test()
def websocket_reports_settled_provider_error_to_browser() -> None:
    """A provider failure ending an empty assistant turn becomes a chat error."""
    fake_runtime = FakeRuntime(
        [
            ModelTurnStarted(),
            MessageSettled(
                reasoning="",
                text="",
                error="Provided authentication token is expired.",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            frame = receive_event(websocket, "error")

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "The model failed while generating a response.")


@test()
def websocket_persists_assistant_message_from_streamed_deltas() -> None:
    """The host assembles streamed text when pi's final event has no content."""
    fake_runtime = FakeRuntime(
        [
            ModelTurnStarted(),
            TextDelta(
                content_index=None, raw_delta={"text": "streamed "}, text="streamed "
            ),
            TextDelta(content_index=None, raw_delta={"text": "answer"}, text="answer"),
            MessageSettled(reasoning="", text=""),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
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
            "turn_queued",
            "user_message",
            "message_start",
            "text_delta",
            "text_delta",
            "message_end",
            "session_status",
            "agent_end",
        ],
    )
    assert_eq(messages[1]["content"], "streamed answer")


@test()
def websocket_drains_stale_events_before_prompt() -> None:
    """Each prompt drains leftover events before driving pi (queue hygiene)."""
    runtime = OrderedRuntime([AgentEnded()])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
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
        _set_runtime_registry(client, FakeRuntimeRegistry(runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            frame = receive_event(websocket, "error")

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "Agent stopped responding during generation.")


@test()
def websocket_persists_reasoning_as_its_own_row_before_the_answer() -> None:
    """Thinking deltas settle into a reasoning row, never merged into the answer."""
    fake_runtime = FakeRuntime(
        [
            ModelTurnStarted(),
            AssistantStreamNote(content_index=0, kind="thinking_start", raw_delta=None),
            ThinkingDelta(
                content_index=0,
                raw_delta={"text": "secret reasoning"},
                text="secret reasoning",
            ),
            TextDelta(content_index=1, raw_delta={"text": "answer"}, text="answer"),
            MessageSettled(reasoning="", text=""),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
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
        service = app_runtime(cast("Starlette", client.app)).conversation_service
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
def websocket_queues_overlapping_prompts_in_fifo_order() -> None:
    """A second prompt waits durably instead of being rejected."""
    fake_runtime = BlockingRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _ = assert_isinstance(client.app, Starlette)
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None

        with client.websocket_connect("/ws") as websocket:
            for content in ("First", "Overlapping"):
                websocket.send_json(
                    {
                        "type": "prompt",
                        "request_id": str(uuid7()),
                        "conversation_id": conversation_id,
                        "content": content,
                    }
                )
            first_user = receive_event(websocket, "user_message")
            portal.call(fake_runtime.events.put, AgentEnded())
            frame = websocket.receive_json()
            while frame.get("event") != "user_message":
                frame = websocket.receive_json()
            portal.call(fake_runtime.events.put, AgentEnded())
            while websocket.receive_json().get("event") != "agent_end":
                pass

    assert_eq(first_user["event"], "user_message")
    assert_eq(frame["event"], "user_message")
    assert_true(first_user["turn_id"] != frame["turn_id"])
    assert_eq(fake_runtime.client.commands, ["prompt", "prompt"])


@test()
def websocket_abort_requires_a_turn_id() -> None:
    """Conversation-wide aborts are rejected before touching a pi runtime."""
    fake_runtime = FakeRuntime([])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "abort", "conversation_id": conversation_id})
            frame = websocket.receive_json()

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "abort turn_id is required")
    assert_eq(fake_runtime.client.commands, [])


@test()
def websocket_abort_is_processed_while_generation_is_running() -> None:
    """The receive loop stays alive while a prompt stream is in flight."""
    fake_runtime = BlockingRuntime()
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Wait for abort",
                }
            )
            first_frame = receive_event(websocket, "user_message")
            websocket.send_json(
                {
                    "type": "abort",
                    "conversation_id": conversation_id,
                    "turn_id": first_frame["turn_id"],
                }
            )
            abort_frame = receive_event(websocket, "abort_ack")

    assert_eq(first_frame["event"], "user_message")
    assert_eq(abort_frame["event"], "abort_ack")
    assert_eq(fake_runtime.client.commands, ["prompt", "abort"])


@test()
def websocket_reports_only_the_confirmed_loaded_skill_count() -> None:
    """Chat receives generic runtime status without skill metadata."""
    fake_runtime = FakeRuntime(
        [AgentEnded()],
        loaded_skills=("grilling", "writing-great-skills"),
        skills_confirmed=True,
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            frames: list[dict[str, Any]] = []
            while True:
                frame = cast("dict[str, Any]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

    status = next(frame for frame in frames if frame.get("event") == "skill_status")
    turn_id = status.pop("turn_id", None)
    assert_true(isinstance(turn_id, str))
    _ = UUID(turn_id)
    assert_eq(
        status,
        {
            "type": "chat",
            "conversation_id": conversation_id,
            "event": "skill_status",
            "loaded_count": 2,
        },
    )


@test()
def websocket_reports_context_usage_for_an_existing_session() -> None:
    """A reconnect can request context state without starting another turn."""
    fake_runtime = FakeRuntime(
        [],
        context_usage=ContextUsage(
            context_window=200_000,
            percent=31.55,
            tokens=63_100,
        ),
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "session_status",
                    "conversation_id": conversation_id,
                }
            )
            frame = websocket.receive_json()

    assert_eq(
        frame,
        {
            "type": "chat",
            "conversation_id": conversation_id,
            "event": "session_status",
            "context_tokens": 63_100,
            "context_window": 200_000,
            "context_percent": 31.55,
        },
    )


@test()
def websocket_reports_context_usage_before_the_turn_closes() -> None:
    """Chat receives pi's context estimate before the terminal turn frame."""
    fake_runtime = FakeRuntime(
        [AgentEnded()],
        context_usage=ContextUsage(
            context_window=200_000,
            percent=31.55,
            tokens=63_100,
        ),
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Hello",
                }
            )
            frames: list[dict[str, Any]] = []
            while True:
                frame = cast("dict[str, Any]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

    status_frames = [
        frame for frame in frames if frame.get("event") == "session_status"
    ]
    turn_id = status_frames[0].pop("turn_id", None)
    assert_true(isinstance(turn_id, str))
    _ = UUID(turn_id)
    assert_eq(
        status_frames,
        [
            {
                "type": "chat",
                "conversation_id": conversation_id,
                "event": "session_status",
                "context_tokens": 63_100,
                "context_window": 200_000,
                "context_percent": 31.55,
            }
        ],
    )
    assert_eq(frames[-1]["event"], "agent_end")


@test()
def websocket_hides_skill_reads_from_live_and_persisted_transcripts() -> None:
    """Progressive disclosure remains internal to pi's agent session."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"path": "/app/apps/agent/skills/grilling/SKILL.md"},
                tool_call_id="call-read",
                tool_name="read",
            ),
            ToolSettled(
                result={"content": "private skill instructions"},
                tool_call_id="call-read",
                tool_name="read",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Grill my plan",
                }
            )
            frame_events: list[object] = []
            while True:
                frame = cast("dict[str, Any]", websocket.receive_json())
                frame_events.append(frame.get("event"))
                if frame.get("event") == "agent_end":
                    break

        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert_eq(
        frame_events,
        [
            "turn_queued",
            "user_message",
            "session_status",
            "error",
            "agent_end",
        ],
    )
    assert_eq([message["role"] for message in messages], ["user"])


@test()
def websocket_marks_tool_only_turns_without_a_final_answer() -> None:
    """A completed turn cannot end in the transcript at the tool row."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"record_type": "walking"},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            ToolSettled(
                result={"records": []},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "look at all the walks these past week",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq(
        [(message["role"], message["content"]) for message in messages],
        [
            ("user", "look at all the walks these past week"),
            ("tool", "query_health_connect"),
            ("assistant", "Turn ended after tool use without a final answer."),
        ],
    )


@test()
def websocket_flags_tool_only_agent_end_frames() -> None:
    """tool_only on agent_end lets the browser skip speaking the marker."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"record_type": "walking"},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            ToolSettled(
                result={"records": []},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "look at all the walks these past week",
                }
            )
            frame: dict[str, object] = {}
            while frame.get("event") != "agent_end":
                frame = websocket.receive_json()

    assert_eq(frame["tool_only"], True)


@test()
def websocket_persists_tool_call_rows() -> None:
    """Tool completion events settle as compact transcript rows."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"content": "tool memory"},
                tool_call_id="call-capture",
                tool_name="capture",
            ),
            ToolSettled(
                result={"details": {"result": {"id": "memory-id"}}},
                tool_call_id="call-capture",
                tool_name="capture",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Use a tool",
                }
            )
            while websocket.receive_json().get("event") != "agent_end":
                pass

        response = client.get(f"/api/conversations/{conversation_id}/messages")

    messages = response.json()
    assert_eq([message["role"] for message in messages], ["user", "tool", "assistant"])
    assert_eq(messages[1]["tool_name"], "capture")
    assert_eq(messages[1]["tool_args"], {"content": "tool memory"})
    assert_eq(messages[1]["tool_result"], {"details": {"result": {"id": "memory-id"}}})
    assert_eq(messages[1]["pi_message_id"], "call-capture")


@test()
def websocket_tool_frames_carry_args_and_result() -> None:
    """Streamed tool frames surface the call input and result for the UI."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"content": "tool memory"},
                tool_call_id="call-capture",
                tool_name="capture",
            ),
            ToolSettled(
                result={"details": {"result": {"id": "memory-id"}}},
                tool_call_id="call-capture",
                tool_name="capture",
            ),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        frames: list[dict[str, Any]] = []
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Use a tool",
                }
            )
            while True:
                frame = cast("dict[str, Any]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

    by_event = {frame.get("event"): frame for frame in frames}
    assert_eq(by_event["tool_start"]["tool_args"], {"content": "tool memory"})
    assert_eq(
        by_event["tool_end"]["tool_result"],
        {"details": {"result": {"id": "memory-id"}}},
    )


@test()
def websocket_compacts_oversized_tool_results_before_chat_completion() -> None:
    """Live transcript frames stay bounded when an agent tool returns huge data."""
    fake_runtime = FakeRuntime(
        [
            ToolStarted(
                args={"record_type": "heart_rate"},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            ToolSettled(
                result={"records": ["x" * 70_000]},
                tool_call_id="call-health",
                tool_name="query_health_connect",
            ),
            MessageSettled(reasoning="", text="Your summary is ready."),
            AgentEnded(),
        ]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        frames: list[dict[str, Any]] = []
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "Summarize my health",
                }
            )
            while True:
                frame = cast("dict[str, Any]", websocket.receive_json())
                frames.append(frame)
                if frame.get("event") == "agent_end":
                    break

    tool_result = next(
        frame["tool_result"] for frame in frames if frame.get("event") == "tool_end"
    )
    assert_eq(tool_result["truncated"], True)
    assert_true(tool_result["original_size_bytes"] > 65_536)
    assert_eq(frames[-1]["event"], "agent_end")


@test()
def transcript_has_no_clear_endpoint() -> None:
    """Conversation lifecycle cannot delete canonical Message Evidence."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        service = app_runtime(cast("Starlette", client.app)).conversation_service
        _ = portal.call(
            service.append_message,
            MessageDraft(
                content="retain me",
                conversation_id=UUID(conversation_id),
                role="user",
            ),
        )

        response = client.delete(f"/api/conversations/{conversation_id}/messages")
        transcript = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert_eq(response.status_code, 405)
    assert_eq([message["content"] for message in transcript], ["retain me"])


@test()
def messages_route_limit_returns_only_the_newest_page() -> None:
    """`?limit=` windows the response to the newest rows, ascending seq."""
    with (
        TemporaryDirectory() as directory,
        make_faux_chat_client(Path(directory)) as client,
    ):
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        for index in range(4):
            prompt_until_agent_end(
                client, conversation_id=conversation_id, content=f"turn {index}"
            )
        full = client.get(f"/api/conversations/{conversation_id}/messages").json()

        response = client.get(
            f"/api/conversations/{conversation_id}/messages", params={"limit": 2}
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), full[-2:])


@test()
def messages_route_before_seq_pages_backwards() -> None:
    """`?limit=&before_seq=` fetches the window just older than a cursor."""
    with (
        TemporaryDirectory() as directory,
        make_faux_chat_client(Path(directory)) as client,
    ):
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        for index in range(4):
            prompt_until_agent_end(
                client, conversation_id=conversation_id, content=f"turn {index}"
            )
        full = client.get(f"/api/conversations/{conversation_id}/messages").json()
        cursor = full[-2]["seq"]
        expected = [message for message in full if message["seq"] < cursor][-2:]

        response = client.get(
            f"/api/conversations/{conversation_id}/messages",
            params={"limit": 2, "before_seq": cursor},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), expected)


@test()
def messages_route_rejects_a_non_positive_limit() -> None:
    """`?limit=0` is a validation error, not a silently-empty page."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        response = client.get(
            f"/api/conversations/{conversation_id}/messages", params={"limit": 0}
        )

    assert_eq(response.status_code, 422)


@test()
def messages_route_rejects_a_non_integer_limit() -> None:
    """`?limit=abc` is a validation error."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]

        response = client.get(
            f"/api/conversations/{conversation_id}/messages", params={"limit": "abc"}
        )

    assert_eq(response.status_code, 422)


@test()
def messages_route_without_params_still_returns_full_history() -> None:
    """No query params keeps the pre-pagination unbounded response."""
    with (
        TemporaryDirectory() as directory,
        make_faux_chat_client(Path(directory)) as client,
    ):
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        for index in range(4):
            prompt_until_agent_end(
                client, conversation_id=conversation_id, content=f"turn {index}"
            )

        unbounded = client.get(f"/api/conversations/{conversation_id}/messages")
        generously_limited = client.get(
            f"/api/conversations/{conversation_id}/messages", params={"limit": 1000}
        )

    assert_eq(unbounded.status_code, 200)
    # At least the 4 user turns must be present; the exact total also depends on
    # how many assistant rows the faux script settles, which isn't this test's
    # concern — the invariant under test is "no params == a sufficiently large
    # limit", i.e. nothing is silently truncated by default.
    assert_true(len(unbounded.json()) >= 4)
    assert_eq(unbounded.json(), generously_limited.json())


@test()
def messages_route_can_filter_one_turn_and_repeats_its_lifecycle() -> None:
    """Flat Message JSON carries a compact repeated turn summary and filter."""
    fake_runtime = FakeRuntime(
        [MessageSettled(reasoning="", text="answer"), AgentEnded()]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "question",
                }
            )
            frame = websocket.receive_json()
            turn_id = frame["turn_id"]
            while frame.get("event") != "agent_end":
                frame = websocket.receive_json()

        response = client.get(
            f"/api/conversations/{conversation_id}/messages",
            params={"turn_id": turn_id},
        )

    assert_eq(response.status_code, 200)
    assert_eq(
        [message["content"] for message in response.json()], ["question", "answer"]
    )
    assert_eq([message["turn_id"] for message in response.json()], [turn_id, turn_id])
    assert_eq(
        [message["turn"] for message in response.json()],
        [
            {
                "failure_code": None,
                "failure_summary": None,
                "intended_fire_at": None,
                "occurrence_id": None,
                "origin": "interactive",
                "status": "succeeded",
                "trigger_id": None,
            }
        ]
        * 2,
    )


@test()
def websocket_translates_unknown_and_archived_conversation_submissions() -> None:
    """Unavailable Conversation targets return public errors without closing WS."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/conversations",
            json={"display_name": "Old", "scope_brief": "Archived work"},
        ).json()
        _ = client.post(f"/api/conversations/{created['id']}/archive")
        with client.websocket_connect("/ws") as websocket:
            details: list[str] = []
            for conversation_id in (str(uuid7()), created["id"]):
                websocket.send_json(
                    {
                        "type": "prompt",
                        "request_id": str(uuid7()),
                        "conversation_id": conversation_id,
                        "content": "unavailable",
                    }
                )
                details.append(receive_event(websocket, "error")["detail"])

    assert_eq(
        details,
        ["conversation is unknown or archived", "conversation is unknown or archived"],
    )


@test()
def websocket_translates_turn_shutdown_without_closing_the_socket() -> None:
    """A stopped turn module reports availability through the chat contract."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        portal = client.portal
        assert portal is not None
        portal.call(
            app_runtime(cast("Starlette", client.app)).conversation_turns.shutdown
        )
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "request_id": str(uuid7()),
                    "conversation_id": conversation_id,
                    "content": "too late",
                }
            )
            frame = receive_event(websocket, "error")

    assert_eq(frame["detail"], "conversation turn execution is unavailable")


@test()
def websocket_prompt_requires_a_browser_request_id() -> None:
    """A prompt without its idempotency UUID is rejected before persistence."""
    fake_runtime = FakeRuntime([AgentEnded()])
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _set_runtime_registry(client, FakeRuntimeRegistry(fake_runtime))
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "prompt",
                    "conversation_id": conversation_id,
                    "content": "missing identity",
                }
            )
            frame = websocket.receive_json()
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert_eq(frame["event"], "error")
    assert_eq(frame["detail"], "prompt request_id is required")
    assert_eq(messages, [])
    assert_eq(fake_runtime.client.commands, [])
