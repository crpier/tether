"""Behavior tests for Memory workspace service wiring."""

from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from snektest import assert_eq, assert_in, test

from tether.memory_workspace_service import (
    MemoryWorkspaceService,
    memory_workspace_root,
)


@test()
async def memory_workspace_service_scans_memory_subdirectory() -> None:
    """Service scans `<kb_root>/memory` and returns canonical topics."""
    logger = structlog.stdlib.get_logger("memory.workspace.service")
    async with TemporaryDirectory() as kb_root:
        root = Path(kb_root)
        memory_root = memory_workspace_root(root)
        memory_root.mkdir(parents=True)
        (memory_root / "topic.md").write_text(
            "\n".join(
                (
                    "---",
                    "title: Service topic",
                    "---",
                    "service body",
                )
            ),
            encoding="utf-8",
        )

        result = await MemoryWorkspaceService(root).scan(logger=logger)

    assert_eq(len(result.topics), 1)
    assert_eq(result.topics[0].title, "Service topic")
    assert_eq(result.diagnostics, [])


@test()
async def memory_workspace_service_produces_diagnostics() -> None:
    """Service returns diagnostics for invalid topic files without crashing."""
    logger = structlog.stdlib.get_logger("memory.workspace.service")
    async with TemporaryDirectory() as kb_root:
        memory_root = Path(kb_root) / "memory"
        memory_root.mkdir(parents=True)
        (memory_root / "bad.md").write_text("no-frontmatter", encoding="utf-8")

        result = await MemoryWorkspaceService(kb_root).scan(logger=logger)

    assert_eq(result.topics, [])
    assert_in(
        "frontmatter.missing_boundary", {item.code for item in result.diagnostics}
    )
