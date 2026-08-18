"""Domain service surface for canonical Memory workspace discovery."""

from __future__ import annotations

from pathlib import Path

from tether.memory_projection import memory_projection_root
from tether.memory_workspace import (
    MemoryWorkspace,
    MemoryWorkspaceDiagnostic,
    MemoryWorkspaceScanResult,
    MemoryWorkspaceTopic,
)
from tether.structured_logging import Logger


def memory_workspace_root(kb_root: str | Path) -> Path:
    """Return the canonical workspace directory under a knowledge-base root."""

    return memory_projection_root(kb_root)


class MemoryWorkspaceService:
    """Expose Memory workspace scanning as a typed service boundary."""

    def __init__(self, kb_root: str | Path) -> None:
        self.kb_root = Path(kb_root)
        self.workspace_root = memory_workspace_root(self.kb_root)
        self.workspace = MemoryWorkspace(self.workspace_root)

    async def scan(self, logger: Logger) -> MemoryWorkspaceScanResult:
        """Scan the canonical workspace for valid topics and diagnostics.

        The workspace service delegates raw filesystem parsing to
        :class:`~tether.memory_workspace.MemoryWorkspace` and logs whether it
        found valid topics so callers can decide how to proceed.
        """
        result = await self.workspace.scan()
        if result.diagnostics:
            logger.debug(
                "Memory workspace scan found diagnostics",
                topic_count=len(result.topics),
                diagnostics_count=len(result.diagnostics),
                diagnostic_codes=tuple(
                    diagnostic.code for diagnostic in result.diagnostics
                ),
            )
        else:
            logger.debug(
                "Memory workspace scan completed",
                topic_count=len(result.topics),
            )
        return result


__all__ = [
    "MemoryWorkspaceDiagnostic",
    "MemoryWorkspaceScanResult",
    "MemoryWorkspaceService",
    "MemoryWorkspaceTopic",
    "memory_workspace_root",
]
