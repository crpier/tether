"""Synthetic panels over Dreaming-maintained Memory Topics."""

import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database, insert
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.dreaming import DreamingMutationCoordinator
from tether.dreaming_store import DreamingWorkspaceFile, create_dreaming_schema
from tether.memory_workspace_service import MemoryWorkspaceService
from tether.panel_errors import InvalidPanelSpecError
from tether.panel_execution import PanelExecutor
from tether.panel_model import PanelSpec
from tether.panel_store import create_panel_schema
from tether.panels import PanelService
from tether.structured_logging import Logger

LOGGER: Logger = structlog.stdlib.get_logger("test.panels")


class PanelEnv:
    """Real panel service and canonical workspace over isolated resources."""

    def __init__(self, database: Database, root: Path) -> None:
        workspace = MemoryWorkspaceService(
            root,
            DreamingMutationCoordinator(database, root / "memory"),
        )
        tracer = trace.NoOpTracerProvider().get_tracer("test.panels")
        self.database = database
        self.workspace = workspace
        self.service = PanelService(
            database,
            PanelExecutor(database, workspace, tracer),
            tracer,
        )

    async def topic(self, path: str, content: str) -> None:
        """Seed one acknowledged Dreaming Topic."""
        target = self.workspace.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        async with self.database.transaction() as transaction:
            _ = await transaction.execute(
                insert(
                    DreamingWorkspaceFile(
                        path=path,
                        content_hash=hashlib.sha256(content.encode()).hexdigest(),
                        content=content,
                        is_tombstone=0,
                        version=1,
                        actor="dream",
                    )
                )
            )


@fixture
async def panel_env() -> AsyncGenerator[PanelEnv]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_dreaming_schema(database)
    await create_panel_schema(database)
    with TemporaryDirectory() as directory:
        yield PanelEnv(database, Path(directory))
    await database.close()


@test()
async def panel_requires_a_query_or_metadata_scope() -> None:
    env = await load_fixture(panel_env())

    with assert_raises(InvalidPanelSpecError):
        _ = await env.service.create(PanelSpec(name="empty", facets={}), logger=LOGGER)


@test()
async def panel_crud_remains_independent_of_memory_rows() -> None:
    env = await load_fixture(panel_env())
    panel = await env.service.create(
        PanelSpec(name="Travel", facets={"domain": "travel"}), logger=LOGGER
    )

    listed = await env.service.list_panels(logger=LOGGER)
    updated = await env.service.update(
        panel,
        PanelSpec(name="Trips", facets={"domain": "travel"}),
        logger=LOGGER,
    )
    deleted = await env.service.delete(updated, logger=LOGGER)

    assert_eq([item.id for item in listed], [panel.id])
    assert_eq(updated.name, "Trips")
    assert_eq(deleted.deleted_at is not None, True)
    assert_eq(await env.service.list_panels(logger=LOGGER), [])


@test()
async def panel_query_reads_current_topics() -> None:
    env = await load_fixture(panel_env())
    await env.topic(
        "travel.md",
        "---\ntitle: Travel\ndomain: travel\n---\nPrefers aisle seats.\n",
    )
    await env.topic(
        "food.md",
        "---\ntitle: Food\ndomain: cooking\n---\nLikes curry.\n",
    )
    panel = await env.service.create(
        PanelSpec(name="Travel", facets={"domain": "travel"}, query="aisle"),
        logger=LOGGER,
    )

    results = await env.service.execute(
        panel,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        logger=LOGGER,
    )

    assert_eq(results.total, 1)
    assert_eq(results.topics[0].title, "Travel")


@test()
async def facets_only_panel_filters_topic_frontmatter() -> None:
    env = await load_fixture(panel_env())
    await env.topic(
        "travel.md",
        "---\ntitle: Travel\ndomain: travel\n---\nPrefers aisle seats.\n",
    )
    panel = await env.service.create(
        PanelSpec(name="Travel", facets={"domain": "travel"}), logger=LOGGER
    )

    results = await env.service.execute(
        panel,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        logger=LOGGER,
    )

    assert_eq(results.total, 1)
    assert_eq(results.topics[0].path.name, "travel.md")
