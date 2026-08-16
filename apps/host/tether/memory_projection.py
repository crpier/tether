"""Markdown projection boundary for canonical SQLite Memories."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from anyio import NamedTemporaryFile, Path
from pydantic import UUID7, BaseModel, DirectoryPath, Field
from snekql.sqlite import Fetched
from yaml import safe_dump, safe_load

from tether.memory_store import Memory, MemoryProvenance


class ProjectionStructureError(Exception):
    """Raised when a projection file is not structured as expected."""


class ProjectionFrontMatter(BaseModel):
    """Persisted metadata decoded from a Memory's Markdown projection."""

    created_at: datetime
    facets: dict[str, str] = Field(default_factory=dict)
    id: UUID7
    provenance: MemoryProvenance
    tethered_at: datetime | None
    updated_at: datetime


def _render_projection_frontmatter(memory: Memory[Fetched]) -> str:
    """Render canonical Memory metadata with projection separators."""
    frontmatter: dict[str, object] = {
        "id": str(memory.id),
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "provenance": memory.provenance,
        "tethered_at": memory.tethered_at,
    }
    if memory.facets:
        frontmatter["facets"] = memory.facets
    return f"---\n{safe_dump(frontmatter)}---\n"


def decode_projection_frontmatter(
    projection_content: str,
) -> ProjectionFrontMatter:
    """Decode projection metadata while rejecting a missing YAML boundary."""
    if not projection_content.startswith("---\n"):
        message = "Frontmatter must start with ---"
        raise ProjectionStructureError(message)
    frontmatter = projection_content[3:].split("---\n", maxsplit=1)[0]
    return ProjectionFrontMatter.model_validate(safe_load(frontmatter))


class MemoryProjection(Protocol):
    """Recoverable Markdown projection operations required by `MemoryService`."""

    kb_root: DirectoryPath

    async def set_projection(self, memory: Memory[Fetched]) -> None: ...

    async def remove_projection(self, memory_id: UUID7) -> None: ...


class KnowledgeBaseService:
    """Manage the recoverable Markdown projection of canonical Memories."""

    def __init__(self, kb_root: DirectoryPath) -> None:
        self.kb_root: DirectoryPath = kb_root

    def projection_path(self, memory_id: UUID7) -> Path:
        """Return the stable `<memory-id>.md` projection path."""
        return Path(self.kb_root / f"{memory_id}.md")

    async def set_projection(self, memory: Memory[Fetched]) -> None:
        """Atomically create or replace one Memory projection."""
        projection_path = self.projection_path(memory.id)
        async with NamedTemporaryFile(
            mode="w", dir=str(projection_path.parent), delete=False
        ) as file:
            temporary_path = Path(file.wrapped.name)
            frontmatter_bytes = await file.write(_render_projection_frontmatter(memory))
            assert frontmatter_bytes != 0
            content_bytes = await file.write(memory.content)
            assert content_bytes != 0
        _ = await temporary_path.replace(projection_path)

    async def remove_projection(self, memory_id: UUID7) -> None:
        """Remove a projection when present; otherwise do nothing."""
        projection_path = self.projection_path(memory_id)
        if await projection_path.exists():
            await projection_path.unlink()
