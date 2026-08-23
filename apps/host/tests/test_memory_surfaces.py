"""Dual-surface behaviour tests for the Memory Review spine.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation; the `/internal/tools/*` endpoints assert the auth
gate and the uniform envelope. Both derive from `tether.memory_capabilities`,
so the service behaviour itself (capture → tether → search invariants) is
exercised once through whichever shell states it most directly.
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID, uuid7

from snekql.sqlite import Database, insert
from snektest import assert_eq, assert_in, assert_not_in, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.surfaces import SESSION, call_tool, login, surface_client
from tether.agent_trace_model import RunCorrelation
from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft
from tether.dreaming import DreamingMutationCoordinator
from tether.dreaming_store import DreamingWorkspaceFile
from tether.openapi_export import build_openapi_document
from tether.search_projection.embeddings import FakeEmbedder


@test()
def public_api_exposes_topics_without_legacy_memory_crud() -> None:
    """The browser reads Dreaming Topics and cannot mutate legacy Memory rows."""
    paths = build_openapi_document()["paths"]

    assert_in("/api/memory-topics", paths)
    assert_not_in("/api/memories", paths)
    assert_not_in("/api/memories/search", paths)
    assert_not_in("/api/memories/{memory_id}", paths)
    assert_not_in("/api/memories/{memory_id}/tether", paths)


def make_client(root: Path) -> Any:
    """A dual-surface app with a `FakeEmbedder` so hybrid search runs offline."""
    return surface_client(root, embedder=FakeEmbedder())


async def _seed_dream_topic(
    database: Database,
    path: str,
    content: str,
) -> None:
    """Seed the last acknowledged Dreaming-authored topic for app restart setup."""
    async with database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamingWorkspaceFile(
                    path=path,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    content=content,
                    is_tombstone=0,
                    version=1,
                    actor="dream",
                )
            )
        )


def _record_dreamed_topic(
    client: TestClient,
    relative_path: str,
    content: str,
) -> None:
    """Record an existing fixture file as acknowledged Dreaming output."""
    portal = client.portal
    assert portal is not None
    portal.call(
        _seed_dream_topic,
        app_runtime(cast("Starlette", client.app)).dreaming_service.database,
        relative_path,
        content,
    )


async def _seed_dream_tombstone(database: Database, path: str) -> None:
    """Seed the last acknowledged Dreaming-authored deletion for restart setup."""
    async with database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamingWorkspaceFile(
                    path=path,
                    content_hash="",
                    content=None,
                    is_tombstone=1,
                    version=2,
                    actor="dream",
                )
            )
        )


async def _record_unacknowledged_dream_topic(
    database: Database,
    workspace_root: Path,
    topic_path: Path,
) -> None:
    """Record a completed Dreaming write without delivering its acknowledgement."""
    _ = await DreamingMutationCoordinator(database, workspace_root).record_mutation(
        run_id=uuid7(),
        tool_call_id="write-topic",
        actor="dream",
        operation="write",
        workspace_path=topic_path,
        payload="dream payload",
    )


async def _record_unacknowledged_dream_deletion(
    database: Database,
    workspace_root: Path,
    topic_path: Path,
) -> None:
    """Record a completed Dreaming deletion without delivering its acknowledgement."""
    _ = await DreamingMutationCoordinator(database, workspace_root).record_mutation(
        run_id=uuid7(),
        tool_call_id="delete-topic",
        actor="dream",
        operation="delete",
        workspace_path=topic_path,
        payload="dream payload",
    )


@test()
def external_edit_cannot_replace_dreamed_memory() -> None:
    """Startup restores Dreaming-authored content before Topic reads."""
    with TemporaryDirectory() as directory:
        app_root = Path(directory)
        memory_root = app_root / ".tether" / "memory"
        topic_path = memory_root / "dream" / "preferences.md"
        dreamed = "---\ntitle: Travel preferences\n---\nPrefers aisle seats.\n"
        manual = "---\ntitle: Travel preferences\n---\nPrefers window seats.\n"

        with make_client(app_root) as client:
            topic_path.parent.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(dreamed, encoding="utf-8")
            portal = client.portal
            assert portal is not None
            portal.call(
                _seed_dream_topic,
                app_runtime(cast("Starlette", client.app)).dreaming_service.database,
                "dream/preferences.md",
                dreamed,
            )

        topic_path.write_text(manual, encoding="utf-8")

        with make_client(app_root) as client:
            login(client)
            response = client.get("/api/memory-topics", params={"q": "seats"})

    assert_eq(response.status_code, 200)
    assert_eq(response.json()[0]["body"], "Prefers aisle seats.\n")


@test()
def live_external_addition_does_not_become_memory() -> None:
    """Topic reads reject a file added while the host is running."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        topic_path = Path(directory) / ".tether" / "memory" / "live-manual.md"
        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text(
            "---\ntitle: Live manual topic\n---\nAdded while running.\n",
            encoding="utf-8",
        )

        login(client)
        response = client.get("/api/memory-topics", params={"q": "manual"})

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), [])


@test()
def external_recreation_cannot_revive_dreamed_deletion() -> None:
    """Startup excludes a manually recreated Dreaming tombstone path."""
    with TemporaryDirectory() as directory:
        app_root = Path(directory)
        memory_root = app_root / ".tether" / "memory"
        topic_path = memory_root / "dream" / "retired.md"

        with make_client(app_root) as client:
            portal = client.portal
            assert portal is not None
            portal.call(
                _seed_dream_tombstone,
                app_runtime(cast("Starlette", client.app)).dreaming_service.database,
                "dream/retired.md",
            )

        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text(
            "---\ntitle: Retired topic\n---\nManually revived.\n",
            encoding="utf-8",
        )

        with make_client(app_root) as client:
            login(client)
            response = client.get("/api/memory-topics", params={"q": "revived"})

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), [])


@test()
def external_deletion_cannot_remove_dreamed_memory() -> None:
    """Startup restores a Dreaming-authored topic deleted outside Dreaming."""
    with TemporaryDirectory() as directory:
        app_root = Path(directory)
        memory_root = app_root / ".tether" / "memory"
        topic_path = memory_root / "dream" / "preferences.md"
        dreamed = "---\ntitle: Travel preferences\n---\nPrefers aisle seats.\n"

        with make_client(app_root) as client:
            topic_path.parent.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(dreamed, encoding="utf-8")
            portal = client.portal
            assert portal is not None
            portal.call(
                _seed_dream_topic,
                app_runtime(cast("Starlette", client.app)).dreaming_service.database,
                "dream/preferences.md",
                dreamed,
            )

        topic_path.unlink()

        with make_client(app_root) as client:
            login(client)
            response = client.get("/api/memory-topics", params={"q": "seats"})

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        [
            {
                "body": "Prefers aisle seats.\n",
                "evidence": [],
                "path": "dream/preferences.md",
                "title": "Travel preferences",
            }
        ],
    )


@test()
def unacknowledged_dream_write_survives_restart() -> None:
    """Startup preserves a completed Dreaming write for acknowledgement retry."""
    with TemporaryDirectory() as directory:
        app_root = Path(directory)
        memory_root = app_root / ".tether" / "memory"
        topic_path = memory_root / "dream" / "new-topic.md"
        dreamed = "---\ntitle: Dreamed topic\n---\nCreated by Dreaming.\n"

        with make_client(app_root) as client:
            topic_path.parent.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(dreamed, encoding="utf-8")
            portal = client.portal
            assert portal is not None
            portal.call(
                _record_unacknowledged_dream_topic,
                app_runtime(cast("Starlette", client.app)).dreaming_service.database,
                memory_root,
                topic_path,
            )

        with make_client(app_root) as client:
            login(client)
            response = client.get("/api/memory-topics", params={"q": "dreamed"})

    assert_eq(response.status_code, 200)
    assert_eq(response.json()[0]["body"], "Created by Dreaming.\n")


@test()
def unacknowledged_dream_deletion_survives_restart() -> None:
    """Startup preserves a completed Dreaming deletion for acknowledgement retry."""
    with TemporaryDirectory() as directory:
        app_root = Path(directory)
        memory_root = app_root / ".tether" / "memory"
        topic_path = memory_root / "dream" / "old-topic.md"
        dreamed = "---\ntitle: Old topic\n---\nRemove through Dreaming.\n"

        with make_client(app_root) as client:
            topic_path.parent.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(dreamed, encoding="utf-8")
            portal = client.portal
            assert portal is not None
            database = app_runtime(
                cast("Starlette", client.app)
            ).dreaming_service.database
            portal.call(
                _seed_dream_topic,
                database,
                "dream/old-topic.md",
                dreamed,
            )
            topic_path.unlink()
            portal.call(
                _record_unacknowledged_dream_deletion,
                database,
                memory_root,
                topic_path,
            )

        with make_client(app_root) as client:
            login(client)
            response = client.get("/api/memory-topics", params={"q": "old"})

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), [])


@test()
def get_workspace_diagnostics_reports_malformed_files() -> None:
    """`GET /api/memory-topics/diagnostics` exposes scan failures."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        root = Path(directory) / ".tether" / "memory"
        root.mkdir(parents=True, exist_ok=True)
        (root / "bad.md").write_text("plain text no frontmatter\n", encoding="utf-8")

        login(client)
        response = client.get("/api/memory-topics/diagnostics")

    assert_eq(response.status_code, 200)
    payload = response.json()
    assert_eq(len(payload), 1)
    assert_eq(payload[0]["code"], "frontmatter.missing_boundary")
    assert_eq(payload[0]["path"], str(root / "bad.md"))


@test()
def get_memory_topics_searches_canonical_workspace() -> None:
    """Canonical Topic files resurface through the Memory API by relevance."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        root = Path(directory) / ".tether" / "memory"
        root.mkdir(parents=True, exist_ok=True)
        gaming = "\n".join(
            (
                "---",
                "title: Gaming preferences",
                "evidence:",
                "  - tether://message/019f0000-0000-7000-8000-000000000001",
                "---",
                "Uses a controller for almost all games.",
            )
        )
        travel = "---\ntitle: Travel\n---\nPrefers aisle seats.\n"
        (root / "gaming.md").write_text(gaming, encoding="utf-8")
        (root / "travel.md").write_text(travel, encoding="utf-8")
        _record_dreamed_topic(client, "gaming.md", gaming)
        _record_dreamed_topic(client, "travel.md", travel)

        login(client)
        response = client.get("/api/memory-topics", params={"q": "controller"})

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        [
            {
                "body": "Uses a controller for almost all games.",
                "evidence": ["tether://message/019f0000-0000-7000-8000-000000000001"],
                "path": "gaming.md",
                "title": "Gaming preferences",
            }
        ],
    )


@test()
def internal_memory_context_resurfaces_relevant_topics() -> None:
    """Foreground pi receives current relevant Topics outside session history."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        root = Path(directory) / ".tether" / "memory"
        root.mkdir(parents=True, exist_ok=True)
        gaming = "---\ntitle: Gaming preferences\n---\nLikes Roboquest.\n"
        travel = "---\ntitle: Travel\n---\nPrefers aisle seats.\n"
        (root / "gaming.md").write_text(gaming, encoding="utf-8")
        (root / "travel.md").write_text(travel, encoding="utf-8")
        _record_dreamed_topic(client, "gaming.md", gaming)
        _record_dreamed_topic(client, "travel.md", travel)

        response = client.post(
            "/internal/memory-context",
            headers={"X-Tether-Tool-Secret": "test-process-secret"},
            json={"query": "What games have I liked?", "session_id": SESSION},
        )

    assert_eq(response.status_code, 200)
    context = response.json()["context"]
    assert_in("Gaming preferences", context)
    assert_in("Likes Roboquest", context)
    assert_not_in("Prefers aisle seats", context)


@test()
def internal_surface_is_absent_from_the_public_openapi() -> None:
    """The tool surface is not described by the public OpenAPI document."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        document = client.get("/openapi.json").json()

    tool_paths = [path for path in document["paths"] if path.startswith("/internal")]
    assert_not_in("/internal/tools/capture", document["paths"])
    assert_eq(tool_paths, [])


@test()
def explicit_remember_tool_targets_the_active_conversation() -> None:
    """Foreground remember/correction intent queues post-turn Dreaming only."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        runtime = app_runtime(cast("Starlette", client.app))
        turn_id = uuid7()
        if client.portal is None:
            raise RuntimeError("test client portal is not running")
        _ = client.portal.call(
            runtime.conversation_service.append_message,
            MessageDraft(
                content="Remember this",
                conversation_id=UUID(conversation_id),
                role="user",
                turn_id=turn_id,
            ),
        )
        _ = runtime.trace_recorder.begin_run(
            session_id=SESSION,
            kind="conversation",
            correlation=RunCorrelation(
                conversation_id=conversation_id,
                origin="interactive",
                turn_id=str(turn_id),
            ),
        )

        envelope = call_tool(client, "queue_memory_assimilation")

        assert_eq(envelope["success"], True)
        assert_eq(envelope["result"], {"queued": True})
        assert_eq(
            runtime.dreaming_service.consume_immediate_assimilation_request(
                UUID(conversation_id)
            ),
            True,
        )


@test()
def scheduled_run_cannot_queue_user_evidence_assimilation() -> None:
    """Scheduled context cannot authorize fresh user-Evidence assimilation."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        runtime = app_runtime(cast("Starlette", client.app))
        _ = runtime.trace_recorder.begin_run(
            session_id=SESSION,
            kind="scheduled",
            correlation=RunCorrelation(
                conversation_id=conversation_id,
                origin="scheduled",
                turn_id=str(uuid7()),
            ),
        )

        envelope = call_tool(client, "queue_memory_assimilation")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"], {"queued": False})


@test()
def search_tool_reads_current_dreaming_topics() -> None:
    """Foreground Search returns current Topics without any mutation surface."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        root = Path(directory) / ".tether" / "memory"
        root.mkdir(parents=True, exist_ok=True)
        topic = "---\ntitle: Travel preferences\n---\nPrefers aisle seats.\n"
        (root / "travel.md").write_text(topic, encoding="utf-8")
        _record_dreamed_topic(client, "travel.md", topic)

        envelope = call_tool(client, "search", q="aisle")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"][0]["path"], "travel.md")
    assert_eq(envelope["result"][0]["body"], "Prefers aisle seats.\n")
