"""Read-only execution of saved Synthetic panels against current Memory Topics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Database, Fetched, select

from tether.dreaming_store import DreamingWorkspaceFile
from tether.memory_workspace import MemoryWorkspaceTopic
from tether.memory_workspace_service import MemoryWorkspaceService
from tether.panel_model import EXECUTE_DEFAULT_LIMIT
from tether.panel_store import SyntheticPanel
from tether.structured_logging import Logger


@dataclass(frozen=True)
class PanelResults:
    """One execution of a panel: capped Topics plus the uncapped match count."""

    topics: list[MemoryWorkspaceTopic]
    total: int


class PanelExecutionPort(Protocol):
    """Execute a stored panel without mutating its saved definition."""

    async def execute(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        now: datetime,
        limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
        logger: Logger,
    ) -> PanelResults:
        """Recompute a panel against current Memory."""
        ...


class PanelExecutor:
    """Recompute saved panel queries over Dreaming-maintained Topics."""

    def __init__(
        self,
        database: Database,
        workspace_service: MemoryWorkspaceService,
        tracer: Tracer,
    ) -> None:
        self.database: Database = database
        self.workspace_service: MemoryWorkspaceService = workspace_service
        self.tracer: Tracer = tracer

    async def execute(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        now: datetime,
        limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
        logger: Logger,
    ) -> PanelResults:
        """Run a panel against current Topics, metadata, and recorded age."""
        after = (
            now - timedelta(days=panel.window_days)
            if panel.window_days is not None
            else None
        )
        with self.tracer.start_as_current_span(
            "PanelService.execute",
            attributes={"panel.id": str(panel.id)},
        ) as span:
            topics = (
                await self.workspace_service.search(
                    panel.query,
                    limit=10_000,
                    logger=logger,
                )
                if panel.query is not None
                else (await self.workspace_service.scan(logger=logger)).topics
            )
            topics = [
                topic for topic in topics if self._matches_facets(topic, panel.facets)
            ]
            if after is not None:
                live_paths = await self._paths_updated_after(after)
                topics = [
                    topic
                    for topic in topics
                    if str(
                        topic.path.relative_to(self.workspace_service.workspace_root)
                    )
                    in live_paths
                ]
            span.set_attribute("panel.execute.total", len(topics))
            logger.debug(
                "Synthetic panel execution completed",
                panel_id=str(panel.id),
                total=len(topics),
            )
            return PanelResults(topics=topics[:limit], total=len(topics))

    @staticmethod
    def _matches_facets(topic: MemoryWorkspaceTopic, facets: dict[str, str]) -> bool:
        """Treat string-valued Topic frontmatter as panel metadata."""
        return all(topic.frontmatter.get(key) == value for key, value in facets.items())

    async def _paths_updated_after(self, after: datetime) -> set[str]:
        """Return current recorded paths changed inside a relative window."""
        async with self.database.transaction() as transaction:
            files = await transaction.fetch_all(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.is_tombstone.eq(0),
                    DreamingWorkspaceFile.updated_at.gte(after),
                )
            )
        return {file.path for file in files}
