"""REST behavior for rebuilding Conversation-derived Memory."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import uuid7

from anyio import Path as AsyncPath
from snektest import assert_eq, assert_len, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft
from tether.dreaming import DreamingMutationCoordinator
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def _make_app(root: Path, *, dreaming_enabled: bool = True) -> Starlette:
    """Create one isolated app with configurable Dreaming."""
    return create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            dreaming_enabled=dreaming_enabled,
            kb_root=root / ".tether",
            session_secret=SESSION_SECRET,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
    )


def _login(client: TestClient) -> None:
    """Authenticate one test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


async def _record_topic(app: Starlette, relative_path: str, content: str) -> Path:
    """Record one acknowledged Dream-authored Topic fixture."""
    runtime = app_runtime(app)
    topic_path = runtime.memory_workspace_service.workspace_root / relative_path
    await AsyncPath(topic_path.parent).mkdir(parents=True, exist_ok=True)
    await AsyncPath(topic_path).write_text(content, encoding="utf-8")
    coordinator = DreamingMutationCoordinator(
        runtime.dreaming_service.database,
        runtime.memory_workspace_service.workspace_root,
    )
    run_id = uuid7()
    _ = await coordinator.record_mutation(
        run_id=run_id,
        tool_call_id="seed-topic",
        actor="dream",
        operation="write",
        workspace_path=topic_path,
        payload="test fixture",
    )
    acknowledged, error = await coordinator.acknowledge_mutation(
        run_id,
        "seed-topic",
    )
    assert_eq(error, None)
    assert_true(acknowledged)
    return topic_path


@test()
def rebuild_requires_authentication() -> None:
    """Anonymous callers cannot invoke the destructive operator action."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 401)


@test()
def rebuild_requires_enabled_dreaming() -> None:
    """A deployment without Dreaming cannot prepare a Memory rebuild."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=False)
        with TestClient(app) as client:
            _login(client)

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 404)


@test()
async def rebuild_rejects_incorrect_confirmation_without_changing_memory() -> None:
    """An operator typo cannot start the destructive rebuild."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            topic_path = await _record_topic(
                cast("Starlette", client.app),
                "preferences.md",
                """---
title: Preferences
evidence:
  - tether://message/019f0927-4fa0-70fa-9847-3edc96296ecf
---

- Likes aisle seats.
""",
            )

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "wrong"},
            )

            assert_eq(response.status_code, 422)
            assert_true(topic_path.exists())


@test()
async def rebuild_preserves_topics_with_non_conversation_evidence() -> None:
    """Typed source Topics survive a Conversation Memory rebuild."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            topic_path = await _record_topic(
                cast("Starlette", client.app),
                "health/activity.md",
                """---
title: Activity
evidence:
  - tether://health-connect/exercise/session-1
---

- Runs twice a week.
""",
            )

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 200)
            assert_true(topic_path.exists())
            assert_eq(response.json()["preserved_topics"], 1)


@test()
async def rebuild_tombstones_conversation_derived_topics() -> None:
    """Rebuild preparation removes current Topics supported only by Messages."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            _ = await _record_topic(
                cast("Starlette", client.app),
                "preferences.md",
                """---
title: Preferences
evidence:
  - tether://message/019f0927-4fa0-70fa-9847-3edc96296ecf
---

- Likes aisle seats.
""",
            )

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 200)
            assert_eq(response.json()["tombstoned_topics"], 1)
            assert_eq(client.get("/api/memory-topics").json(), [])


@test()
async def rebuild_replays_saved_conversations_from_their_first_message() -> None:
    """A rebuilt Conversation queues its previously assimilated Evidence again."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversations = await runtime.conversation_service.list_conversations()
            conversation_id = conversations[0].id
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="I prefer aisle seats",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            first_run = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(first_run.status_code, 200)
            completed = client.post(
                f"/api/dream-runs/{first_run.json()['id']}/complete",
                json={"status": "no_op"},
            )
            assert_eq(completed.status_code, 200)

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 200)
            assert_eq(response.json()["reset_cursors"], 1)
            assert_eq(response.json()["queued_runs"], 1)
            runs = client.get(f"/api/conversations/{conversation_id}/dream-runs").json()
            queued_run = next(run for run in runs if run["status"] == "queued")
            assert_eq(queued_run["evidence_start_seq"], 1)


@test()
async def rebuild_deletions_are_inspectable_in_dream_history() -> None:
    """The rebuild records each replaced Topic as an acknowledged mutation."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            _ = await _record_topic(
                cast("Starlette", client.app),
                "preferences.md",
                """---
title: Preferences
evidence:
  - tether://message/019f0927-4fa0-70fa-9847-3edc96296ecf
---

- Likes aisle seats.
""",
            )

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )
            detail = client.get(f"/api/dream-runs/{response.json()['rebuild_run_id']}")

            assert_eq(detail.status_code, 200)
            assert_eq(detail.json()["run"]["kind"], "rebuild")
            mutations = detail.json()["mutations"]
            assert_len(mutations, 1)
            assert_eq(mutations[0]["operation"], "delete")
            assert_eq(mutations[0]["status"], "acknowledged")
            assert_eq(mutations[0]["workspace_path"], "preferences.md")


@test()
async def rebuild_rejects_active_dreaming_work() -> None:
    """A rebuild cannot invalidate bounds held by an active Dream run."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversations = await runtime.conversation_service.list_conversations()
            conversation_id = conversations[0].id
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="I prefer aisle seats",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            queued = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(queued.status_code, 200)

            response = client.post(
                "/api/memory-rebuilds",
                json={"confirmation": "rebuild-conversation-memory"},
            )

            assert_eq(response.status_code, 409)
            assert_true(
                "active Dream run prevents rebuild" in response.json()["detail"]
            )
