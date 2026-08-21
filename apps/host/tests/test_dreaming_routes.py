"""REST behavior tests for Dream now and dream run completion routes."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from snekql.sqlite import select
from snektest import assert_eq, assert_len, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether.app_runtime import AppRuntime, app_runtime
from tether.conversation_model import MessageDraft
from tether.dreaming import DreamingMutationCoordinator
from tether.dreaming_store import DreamingMutation
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def _make_app(root: Path, *, dreaming_enabled: bool = False) -> Starlette:
    """Create a test app with an isolated database and workspace."""
    return create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret=SESSION_SECRET,
            dreaming_enabled=dreaming_enabled,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
    )


def _login(client: TestClient) -> None:
    """Authenticate the browser session on a scratch test client."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


async def _first_conversation(runtime: AppRuntime) -> UUID:
    """Return the canonical first conversation for a composed runtime."""
    conversations = await runtime.conversation_service.list_conversations()
    return conversations[0].id


@test()
async def manual_dream_now_queues_a_run() -> None:
    """Authenticated users can queue a manual Dream run for a conversation."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="hi",
                    conversation_id=conversation_id,
                    role="user",
                )
            )

            queued = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(queued.status_code, 200)
            body = queued.json()
            assert_eq(body["kind"], "manual")
            assert_eq(UUID(str(body["conversation_id"])), conversation_id)


@test()
async def manual_dream_now_disabled_by_default() -> None:
    """Feature-flagged deployment keeps manual run queueing disabled by default."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)

            denied = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(denied.status_code, 404)


@test()
async def manual_dream_now_returns_204_without_evidence() -> None:
    """Without user evidence, manual queueing cannot open a new run."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)

            empty = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(empty.status_code, 204)


@test()
async def listing_all_dream_runs_shows_history_newest_first() -> None:
    """The global history route exposes every Dream run newest first."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="first settled preference",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            first = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(first.status_code, 200)
            completed = client.post(
                f"/api/dream-runs/{first.json()['id']}/complete",
                json={"status": "no_op"},
            )
            assert_eq(completed.status_code, 200)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="second settled preference",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            second = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(second.status_code, 200)

            runs = client.get("/api/dream-runs")

            assert_eq(runs.status_code, 200)
            payload = runs.json()
            assert_len(payload, 2)
            assert_eq(payload[0]["id"], second.json()["id"])
            assert_eq(payload[0]["conversation_title"], None)
            assert_eq(payload[0]["mutation_count"], 0)
            assert_eq(payload[1]["id"], first.json()["id"])


@test()
async def dream_run_detail_shows_memory_changes() -> None:
    """One run detail explains which canonical Memory paths it changed."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = _make_app(root, dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="I prefer aisle seats",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            queued = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(queued.status_code, 200)
            run_id = UUID(queued.json()["id"])
            workspace = root / "memory"
            coordinator = DreamingMutationCoordinator(
                runtime.dreaming_service.database,
                workspace,
            )
            _ = await coordinator.record_mutation(
                run_id=run_id,
                tool_call_id="tool-write-preferences",
                actor="dream",
                operation="write",
                workspace_path=workspace / "preferences.md",
                payload="updated preferences",
            )

            detail = client.get(f"/api/dream-runs/{run_id}")

            assert_eq(detail.status_code, 200)
            payload = detail.json()
            assert_eq(payload["run"]["id"], str(run_id))
            assert_eq(payload["run"]["mutation_count"], 1)
            assert_len(payload["mutations"], 1)
            assert_eq(payload["mutations"][0]["operation"], "write")
            assert_eq(payload["mutations"][0]["workspace_path"], "preferences.md")
            assert_eq(payload["mutations"][0]["status"], "executed")
            assert_true("payload" not in payload["mutations"][0])


@test()
async def listing_dream_runs_shows_queued_runs() -> None:
    """A conversation route exposes dream runs in newest-first order."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="hi",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            queued = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(queued.status_code, 200)

            runs = client.get(f"/api/conversations/{conversation_id}/dream-runs")
            assert_eq(runs.status_code, 200)
            payload = runs.json()
            assert_len(payload, 1)
            assert_eq(payload[0]["kind"], "manual")


@test()
async def completing_run_marks_terminal() -> None:
    """An external callback can mark a dream run terminal."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="hi",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            queued = client.post(f"/api/conversations/{conversation_id}/dream-now")
            assert_eq(queued.status_code, 200)
            queued_body = queued.json()
            run_id = UUID(str(queued_body["id"]))

            completed = client.post(
                f"/api/dream-runs/{run_id}/complete",
                json={"status": "success"},
            )
            assert_eq(completed.status_code, 200)
            complete_body = completed.json()
            assert_eq(complete_body["status"], "success")
            assert_eq(UUID(str(complete_body["conversation_id"])), conversation_id)


@test()
def malformed_conversation_id_rejects_as_not_found() -> None:
    """Malformed UUIDs on dream run queue endpoints never crash."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)

            queued = client.post("/api/conversations/not-a-conversation/dream-now")
            assert_eq(queued.status_code, 404)

            runs = client.get(
                "/api/conversations/not-a-conversation/dream-runs",
            )
            assert_eq(runs.status_code, 404)


@test()
def malformed_run_id_rejects_as_not_found() -> None:
    """Malformed UUIDs on run completion never crash and return a 404."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            _login(client)

            not_found = client.post(
                "/api/dream-runs/not-a-run/complete",
                json={"status": "success"},
            )
            assert_eq(not_found.status_code, 404)


@test()
async def complete_run_rejected_when_dreaming_disabled() -> None:
    """Disabled deployment does not expose manual run completion."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="sleepy",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            denied = client.post(
                f"/api/dream-runs/{run.id}/complete",
                json={"status": "success"},
            )
            assert_eq(denied.status_code, 404)
            assert_eq(denied.json()["detail"], "dreaming not enabled")


@test()
async def mutation_ack_route_acknowledges_recorded_mutation() -> None:
    """PI callback can acknowledge a Dream mutation with deterministic identity."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="note",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            workspace_root = runtime.memory_workspace_service.workspace_root
            target = workspace_root / str(run.conversation_id)
            target.mkdir(parents=True)
            file_path = target / f"{run.id}.md"
            file_path.write_text("# draft", encoding="utf-8")

            coordinator = DreamingMutationCoordinator(
                runtime.dreaming_service.database,
                workspace_root,
            )
            tool_call_id = coordinator.mutation_tool_call_id(run)
            inserted = await coordinator.record_mutation(
                run_id=run.id,
                tool_call_id=tool_call_id,
                actor="dream",
                operation="write",
                workspace_path=file_path,
                payload="{}",
            )
            assert inserted is not None

            response = client.post(
                f"/internal/dream-runs/{run.id}/mutations/{tool_call_id}/ack",
                headers={"X-Tether-Tool-Secret": runtime.tool_secret},
            )
            assert_eq(response.status_code, 200)
            body = response.json()
            assert_eq(body["run_id"], str(run.id))
            assert_eq(body["tool_call_id"], tool_call_id)
            assert_eq(body["acknowledged"], True)

            again = client.post(
                f"/internal/dream-runs/{run.id}/mutations/{tool_call_id}/ack",
                headers={"X-Tether-Tool-Secret": runtime.tool_secret},
            )
            assert_eq(again.status_code, 200)

            async with runtime.dreaming_service.database.transaction() as tx:
                mutation = await tx.fetch_one_or_none(
                    select(DreamingMutation)
                    .where(DreamingMutation.tool_call_id.eq(tool_call_id))
                    .where(DreamingMutation.run_id.eq(run.id))
                )
            assert mutation is not None
            assert_eq(mutation.status, "acknowledged")
            assert_eq(mutation.error, None)


@test()
async def mutation_ack_is_not_authorized_without_tool_secret() -> None:
    """Mutation ACK endpoint enforces the same tool-secret contract as tools."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="secretless",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            denied = client.post(
                f"/internal/dream-runs/{run.id}/mutations/any-id/ack",
                headers={"X-Tether-Tool-Secret": "bad"},
            )
            assert_eq(denied.status_code, 401)
            assert_eq(denied.json()["detail"], "invalid tool secret")


@test()
async def mutation_ack_is_disabled_when_dreaming_not_configured() -> None:
    """ACK endpoints honor global dreaming feature flags."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=False)
        with TestClient(app) as client:
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="no dreaming",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            workspace_root = runtime.memory_workspace_service.workspace_root
            target = workspace_root / str(run.conversation_id)
            target.mkdir(parents=True)
            file_path = target / f"{run.id}.md"
            file_path.write_text("# draft", encoding="utf-8")
            coordinator = DreamingMutationCoordinator(
                runtime.dreaming_service.database,
                workspace_root,
            )
            tool_call_id = coordinator.mutation_tool_call_id(run)
            _ = await coordinator.record_mutation(
                run_id=run.id,
                tool_call_id=tool_call_id,
                actor="dream",
                operation="write",
                workspace_path=file_path,
                payload="{}",
            )

            denied = client.post(
                f"/internal/dream-runs/{run.id}/mutations/{tool_call_id}/ack",
                headers={"X-Tether-Tool-Secret": runtime.tool_secret},
            )
            assert_eq(denied.status_code, 404)
            assert_eq(denied.json()["detail"], "dreaming not enabled")


@test()
async def mutation_ack_fails_when_missing_mutation() -> None:
    """Callback retries return 404 when no executed mutation is found."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="orphan",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            not_found = client.post(
                f"/internal/dream-runs/{run.id}/mutations/nope/ack",
                headers={"X-Tether-Tool-Secret": runtime.tool_secret},
            )
            assert_eq(not_found.status_code, 404)
            assert_eq(not_found.json()["detail"], "mutation not found")


@test()
async def mutation_ack_returns_500_on_coordinator_error() -> None:
    """Coordinator failures return transport-safe HTTP 500 on callback."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            runtime = app_runtime(cast("Starlette", client.app))
            conversation_id = await _first_conversation(runtime)
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="broken file",
                    conversation_id=conversation_id,
                    role="user",
                )
            )
            run = await runtime.dreaming_service.queue_manual_run(
                conversation_id,
                logger=runtime.logger,
                now=datetime.now(UTC),
            )
            assert run is not None

            workspace_root = runtime.memory_workspace_service.workspace_root
            target = workspace_root / str(run.conversation_id)
            target.mkdir(parents=True)
            file_path = target / f"{run.id}.md"
            file_path.write_bytes(b"\xff\xfe")

            coordinator = DreamingMutationCoordinator(
                runtime.dreaming_service.database,
                workspace_root,
            )
            tool_call_id = coordinator.mutation_tool_call_id(run)
            inserted = await coordinator.record_mutation(
                run_id=run.id,
                tool_call_id=tool_call_id,
                actor="dream",
                operation="write",
                workspace_path=file_path,
                payload="{}",
            )
            assert inserted is not None

            response = client.post(
                f"/internal/dream-runs/{run.id}/mutations/{tool_call_id}/ack",
                headers={"X-Tether-Tool-Secret": runtime.tool_secret},
            )
            assert_eq(response.status_code, 500)
            assert_true(response.json()["detail"].startswith("UnicodeDecodeError:"))


@test()
def malformed_dream_mutation_ack_path_rejects_as_not_found() -> None:
    """Malformed run IDs on mutation ACK endpoints never crash."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory), dreaming_enabled=True)
        with TestClient(app) as client:
            response = client.post(
                "/internal/dream-runs/not-a-run/mutations/bad/ack",
                headers={"X-Tether-Tool-Secret": "whatever"},
            )
            assert_eq(response.status_code, 404)
