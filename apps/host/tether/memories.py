"""Memory spine service layer: Capture, Review (tether / edit / reject), Search.

A Memory is Captured `loose`, becomes `tethered` once a human vets it, and
is soft-deleted on reject. The load-bearing invariant is that the assistant
Searches only tethered, non-deleted Memories.

>>> service = MemoryService(database=database, kb_root=kb_root)
>>> memory = await service.capture("I prefer aisle seats")
>>> tethered = await service.tether(memory)
>>> [hit.content for hit in await service.search("aisle")]
['I prefer aisle seats']
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict
from uuid import uuid7

import structlog
from anyio import NamedTemporaryFile, Path
from pydantic import UUID7, BaseModel, DirectoryPath, Json, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    scaffold,
    select,
    update,
)
from yaml import safe_dump, safe_load

logger = structlog.stdlib.get_logger(__name__)

type MemoryState = Literal["loose", "tethered"]
"""A Memory's trust state. `deleted` is an orthogonal soft-delete marker, not a state."""


class MemoryProvenance(TypedDict):
    kind: Literal["manual"]


class MemoryNotFoundError(Exception):
    """Raised when an operation targets a Memory that is absent, soft-delete,
    or otherwise doesn't meet invariant requirements of an operation

    E.g. the operation can only be applied on tethered memories and but the
    target is a loose memory"""


class MemoryConflictError(Exception):
    """Raised when a live Memory exists but cannot accept the requested operation.

    This is a domain-state conflict, not absence: e.g. tethering an already
    tethered Memory.
    """


class EmptySearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class EmptyMemoryContentError(Exception):
    """Raised when Memory content is blank after trimming whitespace."""


class ProjectionStructureError(Exception):
    """Raised when a projection file is not structured as expected."""


def _normalise_content(content: str) -> str:
    """Trim captured or edited content while preserving required content.

    Memory content is the amorphous fact itself, so surrounding whitespace is
    capture noise. An empty fact after trimming is not a Memory.
    """
    normalised_content = content.strip()
    if not normalised_content:
        msg = "Memory content must not be blank"
        raise EmptyMemoryContentError(msg)
    return normalised_content


class Memory[S = Pending](Model[S, "Memory[Fetched]"]):
    id: Memory.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    content: Memory.Col[str] = Text()
    """The actual content of the Memory."""
    version: Memory.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    provenance: Memory.Col[Json[MemoryProvenance]] = Text(
        default_factory=lambda: MemoryProvenance(kind="manual"),
    )
    """The origin of a Captured Memory."""
    created_at: Memory.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: Memory.GenCol[datetime] = Text(default=CurrentTimestamp)
    tethered_at: Memory.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )
    deleted_at: Memory.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )


class FrontMatter(BaseModel):
    id: UUID7
    created_at: datetime
    updated_at: datetime
    provenance: MemoryProvenance
    tethered_at: datetime | None


class FrontMatterConversion:
    # TODO: double-check that this approach makes sense
    @staticmethod
    def generate_frontmatter(memory: Memory[Fetched]) -> str:
        """Generate a string containing the YAML frontmatter for a Memory,
        including its separators."""
        yaml_string = safe_dump(
            {
                "id": str(memory.id),
                "created_at": memory.created_at,
                "updated_at": memory.updated_at,
                "provenance": memory.provenance,
                "tethered_at": memory.tethered_at,
            }
        )
        return f"---\n{yaml_string}---\n"

    # TODO: I'm a bit sad about how inconsistent the API of these 2 methods is
    @staticmethod
    def retrieve_frontmatter(projection_content: str) -> FrontMatter:
        # Use of `\n` means this only works on Unix-style newlines. That's good.
        if not projection_content.startswith("---\n"):
            msg = "Frontmatter must start with ---"
            raise ProjectionStructureError(msg)
        frontmatter = projection_content[3:].split("---\n", maxsplit=1)[0]
        projection_content = safe_load(frontmatter)
        return FrontMatter.model_validate(projection_content)


class KnowledgeBaseService:
    """Capability for managing the Markdown file aspect of the Memory."""

    def __init__(self, kb_root: DirectoryPath) -> None:
        self.kb_root: DirectoryPath = kb_root

    def projection_path(self, memory_id: UUID7) -> Path:
        """Projection path for a Memory: `<id>.md`.

        Named by the Memory's UUIDv7 — an opaque stable id, not a content slug,
        so the basename never changes when content is edited."""
        return Path(self.kb_root / f"{memory_id}.md")

    async def set_projection(self, memory: Memory[Fetched]) -> None:
        """Write a Memory's content to its projection file.
        Can both create and edit a Memory's projection file."""
        projection_path = self.projection_path(memory.id)
        async with NamedTemporaryFile(
            mode="w", dir=str(projection_path.parent), delete=False
        ) as file:
            temp_path = Path(file.wrapped.name)
            frontmatter = FrontMatterConversion.generate_frontmatter(memory)
            bytes_written = await file.write(frontmatter)
            assert bytes_written != 0
            bytes_written = await file.write(memory.content)
            assert bytes_written != 0
        _ = await temp_path.replace(projection_path)

    async def remove_projection(self, memory_id: UUID7) -> None:
        """Remove a Memory's projection file. If it doesn't have one, do nothing."""
        path = self.projection_path(memory_id)
        if await path.exists():
            await path.unlink()


class MemoryService:
    """Capability surface for the Memory Review spine, over a snekql database.

    Each method owns its own transaction (one mutation, one commit). Mutations
    return the resulting Memory so the REST layer can echo it."""

    def __init__(self, database: Database, kb_service: KnowledgeBaseService) -> None:
        self.database: Database = database
        self.kb_service: KnowledgeBaseService = kb_service

    async def capture(self, content: str) -> Memory[Fetched]:
        """Capture a loose Memory from content.
        Always lands `loose` — there is no direct-to-tethered path."""
        normalised_content = _normalise_content(content)
        logger.debug("Capturing Memory", content_length=len(normalised_content))
        async with self.database.transaction() as tx:
            memory = await tx.execute(
                insert(Memory(content=normalised_content)).returning()
            )
        logger.info(
            "Memory captured",
            memory_id=str(memory.id),
            version=memory.version,
        )
        return memory

    async def search(
        self, query: str, limit: PositiveInt = 50
    ) -> list[Memory[Fetched]]:
        """Keyword Search the assistant uses to pull context.

        Placeholder matcher: the query is split into whitespace terms, each
        matched case-insensitively with `LIKE` and AND-ed; results are
        tethered-only, newest-first, unranked, capped at `limit` (default 50).
        Because the order is recency, the cap keeps the newest matches."""
        terms = query.split()
        if not terms:
            msg = "keyword Search requires a non-empty query"
            raise EmptySearchQueryError(msg)
        logger.debug("Searching Memories", terms_count=len(terms), limit=limit)
        tethered_matches = select(Memory).where(
            Memory.tethered_at.is_not_null() & Memory.deleted_at.is_null()
        )
        for term in terms:
            tethered_matches = tethered_matches.where(Memory.content.like(f"%{term}%"))
        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(
                tethered_matches.order_by(Memory.created_at.desc()).limit(limit)
            )
        logger.debug(
            "Memory Search completed",
            terms_count=len(terms),
            limit=limit,
            result_count=len(memories),
        )
        return memories

    async def browse_by_state(self, state: MemoryState) -> list[Memory[Fetched]]:
        """Filter-only Search backing the human review UI (`GET /memories?state=`).

        Human-facing, not assistant-facing: unlike keyword `search` it is not
        bound by the tethered-only invariant — `loose` deliberately returns the
        review queue. Soft-deleted Memories are always excluded. `loose` is
        newest-first (fresh captures reviewed while context is warm); `tethered`
        is ordered by `tethered_at` desc (most recently trusted first)."""
        logger.debug("Browsing Memories by state", state=state)
        live = select(Memory).where(Memory.deleted_at.is_null())
        match state:
            case "loose":
                browse = live.where(Memory.tethered_at.is_null()).order_by(
                    Memory.created_at.desc()
                )
            case "tethered":
                browse = live.where(Memory.tethered_at.is_not_null()).order_by(
                    Memory.tethered_at.desc()
                )
        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(browse)
        logger.debug(
            "Memory browse completed",
            state=state,
            result_count=len(memories),
        )
        return memories

    # TODO: re-evaluate whether passing primitives instead of objects around
    # was the right call
    async def tether(self, memory: Memory[Fetched]) -> Memory[Fetched]:
        """Promote a loose Memory to tethered, making it Searchable.

        Tethering an already-tethered Memory conflicts. Tethering an absent or
        deleted Memory raises."""
        logger.debug(
            "Tethering Memory",
            memory_id=str(memory.id),
            observed_version=memory.version,
        )
        async with self.database.transaction() as tx:
            matched_rows = await tx.execute(
                update(Memory)
                .set(Memory.tethered_at.to(CurrentTimestamp))
                .set(Memory.version.to(memory.version + 1))
                .where(Memory.id.eq(memory.id))
                .where(Memory.deleted_at.is_null())
                .where(Memory.tethered_at.is_null())
                .where(Memory.version.eq(memory.version))
            )
            fresh_memory = await self._fetch_active(tx, memory.id)
            if matched_rows == 0:
                if fresh_memory.tethered_at is not None:
                    logger.debug(
                        "Memory tether conflict",
                        memory_id=str(memory.id),
                        reason="already_tethered",
                        observed_version=memory.version,
                        current_version=fresh_memory.version,
                    )
                    msg = f"Memory {memory.id} is already tethered"
                    raise MemoryConflictError(msg)
                if fresh_memory.version != memory.version:
                    logger.debug(
                        "Memory tether conflict",
                        memory_id=str(memory.id),
                        reason="stale_version",
                        observed_version=memory.version,
                        current_version=fresh_memory.version,
                    )
                    msg = f"Tried to update memory {memory.id} with version {memory.version} but had version {fresh_memory.version}"
                    raise MemoryConflictError(msg)
        await self._try_set_projection(fresh_memory)
        logger.info(
            "Memory tethered",
            memory_id=str(fresh_memory.id),
            previous_version=memory.version,
            version=fresh_memory.version,
        )
        return fresh_memory

    async def edit_content(
        self, memory: Memory[Fetched], content: str
    ) -> Memory[Fetched]:
        """Edit a Memory's content and bump `updated_at`.

        Authorship gates trust: a human edit *is* the review, so a
        tethered Memory stays tethered (its projection refreshes) and a loose one
        stays loose. Editing an absent or deleted Memory raises.
        """
        normalised_content = _normalise_content(content)
        logger.debug(
            "Editing Memory content",
            memory_id=str(memory.id),
            observed_version=memory.version,
            content_length=len(normalised_content),
        )
        async with self.database.transaction() as tx:
            matched_rows = await tx.execute(
                update(Memory)
                .set(Memory.content.to(normalised_content))
                .set(Memory.updated_at.to(CurrentTimestamp))
                .set(Memory.version.to(memory.version + 1))
                .where(Memory.id.eq(memory.id))
                .where(Memory.deleted_at.is_null())
                .where(Memory.version.eq(memory.version))
            )
            fresh_memory = await self._fetch_active(tx, memory.id)
            if matched_rows == 0:
                # Earlier, we fetched an active Memory. If we're here, it's
                # because the version was stale.
                logger.debug(
                    "Memory edit conflict",
                    memory_id=str(memory.id),
                    reason="stale_version",
                    observed_version=memory.version,
                    current_version=fresh_memory.version,
                )
                msg = f"Tried to edit memory {memory.id} with version {memory.version} but had version {fresh_memory.version}"
                raise MemoryConflictError(msg)

        # An invariant is that loose memories don't have projections
        if fresh_memory.tethered_at is not None:
            await self._try_set_projection(fresh_memory)
        logger.info(
            "Memory content edited",
            memory_id=str(fresh_memory.id),
            previous_version=memory.version,
            version=fresh_memory.version,
            tethered=fresh_memory.tethered_at is not None,
        )
        return fresh_memory

    async def delete(self, memory: Memory[Fetched]) -> Memory[Fetched]:
        """Reject a Memory by soft-deleting it: stamp `deleted_at`, retain the row.

        All deletions are soft regardless of state, so a rejected Memory stays
        recoverable in the DB while dropping out of every queue, the assistant's
        Search, and the KB. Deleting an absent or already-deleted Memory raises.
        """
        logger.debug(
            "Deleting Memory",
            memory_id=str(memory.id),
            observed_version=memory.version,
        )
        async with self.database.transaction() as tx:
            # TODO: reduce number of queries by using the return value of the `execute` method
            rows_matched = await tx.execute(
                update(Memory)
                .set(Memory.deleted_at.to(CurrentTimestamp))
                .set(Memory.version.to(memory.version + 1))
                .where(Memory.id.eq(memory.id))
                .where(Memory.deleted_at.is_null())
                .where(Memory.version.eq(memory.version))
            )
            deleted_memory = await tx.fetch_one_or_none(
                select(Memory).where(Memory.id.eq(memory.id))
            )
            if deleted_memory is None:
                raise MemoryNotFoundError(memory.id)

            if rows_matched == 0:
                logger.debug(
                    "Memory delete conflict",
                    memory_id=str(memory.id),
                    reason="already_deleted_or_stale_version",
                    observed_version=memory.version,
                    current_version=deleted_memory.version,
                )
                msg = f"Memory {memory.id} is already deleted"
                raise MemoryConflictError(msg)

        await self._try_remove_projection(memory.id)
        logger.info(
            "Memory deleted",
            memory_id=str(deleted_memory.id),
            previous_version=memory.version,
            version=deleted_memory.version,
            was_tethered=deleted_memory.tethered_at is not None,
        )
        return deleted_memory

    async def regenerate_knowledge_base(self) -> None:
        """Rebuild the Knowledge base projection from live SQLite state.

        This is the recovery path for any post-commit projection write that
        failed during a mutation: SQLite remains the source of truth, and the
        markdown projection can be derived again.
        """
        logger.debug("Regenerating Knowledge base")
        async with self.database.transaction() as tx:
            tethered_memories = await tx.fetch_all(
                select(Memory).where(
                    Memory.tethered_at.is_not_null() & Memory.deleted_at.is_null()
                )
            )
        expected_filenames = {f"{memory.id}.md" for memory in tethered_memories}
        removed_count = 0
        async for path in Path(self.kb_service.kb_root).iterdir():
            if path.suffix == ".md" and path.name not in expected_filenames:
                logger.debug("Removing stale projection", projection_path=str(path))
                await path.unlink()
                removed_count += 1
        for memory in tethered_memories:
            logger.debug("Writing Knowledge base projection", memory_id=str(memory.id))
            await self.kb_service.set_projection(memory)
        logger.info(
            "Knowledge base regenerated",
            projected_count=len(tethered_memories),
            removed_count=removed_count,
        )

    async def _try_set_projection(self, memory: Memory[Fetched]) -> None:
        """Log post-commit projection failures without hiding the DB write."""
        logger.debug("Writing Memory projection", memory_id=str(memory.id))
        try:
            await self.kb_service.set_projection(memory)
        except Exception:
            logger.exception("Failed to project Memory", memory_id=str(memory.id))

    async def _try_remove_projection(self, memory_id: UUID7) -> None:
        """Log post-commit projection removal failures after soft-delete."""
        logger.debug("Removing Memory projection", memory_id=str(memory_id))
        try:
            await self.kb_service.remove_projection(memory_id)
        except Exception:
            logger.exception(
                "Failed to remove Memory projection",
                memory_id=str(memory_id),
            )

    async def _fetch_active(self, tx: Transaction, memory_id: UUID7) -> Memory[Fetched]:
        """Fetch a non-deleted Memory by id or raise; the guard every mutation shares.

        Centralizing it keeps "operate only on a live Memory" identical across
        tether, edit, and delete instead of re-deriving the soft-delete check.
        """
        memory = await tx.fetch_one_or_none(
            select(Memory).where(Memory.id.eq(memory_id), Memory.deleted_at.is_null())
        )
        if memory is None:
            raise MemoryNotFoundError(memory_id)
        return memory


async def create_memory_schema(database: Database) -> None:
    """Create the Memory table on an already-initialized database.

    snekql builds schema by replaying migrations rather than from models
    directly, so the scaffolded DDL for `Memory` is applied as a single
    migration. The caller owns `Database.initialize` (and thus the backend
    choice) and hands the live database here before serving requests.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_memory_schema(database)
    """
    await database.migrate({"001_memories": scaffold([Memory])})
