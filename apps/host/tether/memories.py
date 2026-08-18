"""Memory capture, Review mutations, facets, and projection coordination."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, cast

from anyio import Path as AsyncPath
from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.memory_projection import (
    MemoryProjection,
    is_managed_projection,
    memory_projection_root,
)
from tether.memory_store import Memory, MemoryProvenance, tethered_corpus
from tether.structured_logging import Logger


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


class EmptyMemoryContentError(Exception):
    """Raised when Memory content is blank after trimming whitespace."""


class FacetOverviewEntry(BaseModel):
    """One distinct `(key, value)` facet pair and how many Memories carry it.

    >>> FacetOverviewEntry(key="sensitivity", value="private", count=3).count
    3
    """

    key: str
    value: str
    count: PositiveInt


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


def _exception(logger: Logger, event: str, **context: object) -> None:
    """Emit an exception event using caller-supplied logging context."""
    logger.exception(event, **context)


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


def _agent_routed_append_block(content: str, appended_at: datetime) -> str:
    """Format a content-preserving append block for agent-routed placement.

    The user's words stay verbatim inside the block; the heading carries the
    timestamp and provenance marker a human needs to spot a bad route later.
    """
    return (
        f"\n\n---\n\n### agent-routed append — {appended_at.isoformat()}\n\n{content}"
    )


class MemoryIndexProjection(Protocol):
    """Derived Search index writes performed after canonical SQLite commits."""

    async def index_memory(
        self, memory: Memory[Fetched], *, logger: Logger
    ) -> None: ...
    async def deindex_memory(self, memory_id: UUID7, *, logger: Logger) -> None: ...


class MemoryService:
    """Capability surface for the Memory Review spine, over a snekql database.

    Each method owns its own transaction (one mutation, one commit). Mutations
    return the resulting Memory so the REST layer can echo it."""

    def __init__(
        self,
        database: Database,
        kb_service: MemoryProjection,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
        indexer: MemoryIndexProjection | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.indexer: MemoryIndexProjection | None = indexer
        self.kb_service: MemoryProjection = kb_service
        self.tracer: Tracer = tracer

    async def capture(
        self,
        content: str,
        *,
        provenance: MemoryProvenance | None = None,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Capture a loose Memory from content.

        Always lands `loose` — there is no direct-to-tethered path. `provenance`
        defaults to manual; a non-manual producer (import, YouTube, web) passes
        its own origin so downstream Review can calibrate scrutiny and grouping.
        `facets` defaults to an empty Commons facet set (`{}`) when omitted, and
        is persisted verbatim otherwise.
        """
        normalised_content = _normalise_content(content)
        memory_provenance = (
            provenance if provenance is not None else MemoryProvenance(kind="manual")
        )
        memory_facets = facets if facets is not None else {}
        with self.tracer.start_as_current_span(
            "MemoryService.capture",
            attributes={"memory.content_length": len(normalised_content)},
        ) as span:
            _debug(logger, "Capturing Memory", content_length=len(normalised_content))

            async def _capture(tx: Transaction) -> Memory[Fetched]:
                return await tx.execute(
                    insert(
                        Memory(
                            content=normalised_content,
                            provenance=memory_provenance,
                            facets=memory_facets,
                        )
                    ).returning()
                )

            async with self.database.transaction(mode="immediate") as tx:
                memory = await _capture(tx)
            span.set_attribute("memory.id", str(memory.id))
            span.set_attribute("memory.version", memory.version)
            _info(
                logger,
                "Memory captured",
                memory_id=str(memory.id),
                version=memory.version,
            )
            await self.event_publisher.publish(
                InvalidateEvent(keys=["memories", "review-queue"])
            )
            return memory

    async def capture_tethered(
        self,
        content: str,
        *,
        provenance: MemoryProvenance,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Capture a machine-synced Memory that is trusted at insert.

        The direct-to-tethered path for the machine-synced provenance class: an
        Ingestion gate writing content verbatim from an external system of record
        (a Readwise highlight, a calendar event). Unlike `capture`, the Memory
        lands with `tethered_at` stamped, so it never enters the loose queue or
        Review and is Searchable at once — the sync itself is the assertion of
        fact, nothing is invented. Its projection and search-index entry are
        written immediately, exactly as a tether would. `provenance` names the
        syncing origin (never `manual`); `facets` default to the empty Commons
        set (`{}`) and are persisted verbatim otherwise.
        """
        normalised_content = _normalise_content(content)
        memory_facets = facets if facets is not None else {}
        with self.tracer.start_as_current_span(
            "MemoryService.capture_tethered",
            attributes={"memory.content_length": len(normalised_content)},
        ) as span:
            _debug(
                logger,
                "Capturing tethered Memory",
                content_length=len(normalised_content),
            )

            async def _capture(tx: Transaction) -> Memory[Fetched]:
                inserted = await tx.execute(
                    insert(
                        Memory(
                            content=normalised_content,
                            provenance=provenance,
                            facets=memory_facets,
                        )
                    ).returning()
                )
                # Stamp `tethered_at` from the DB clock in the same transaction so
                # the row is never observable in a loose state — machine-synced
                # content skips the loose→tethered gate entirely.
                _ = await tx.execute(
                    update(Memory)
                    .set(Memory.tethered_at.to(CurrentTimestamp))
                    .where(Memory.id.eq(inserted.id))
                )
                return await self._fetch_active(tx, inserted.id)

            async with self.database.transaction(mode="immediate") as tx:
                memory = await _capture(tx)
            span.set_attribute("memory.id", str(memory.id))
            span.set_attribute("memory.provenance_kind", provenance["kind"])
            _info(
                logger,
                "Tethered Memory captured",
                memory_id=str(memory.id),
                provenance_kind=provenance["kind"],
            )
            await self._try_set_projection(memory, logger=logger)
            await self._try_index(memory, logger=logger)
            await self.event_publisher.publish(
                InvalidateEvent(keys=["memories", "review-queue"])
            )
            return memory

    async def tether(
        self,
        memory: Memory[Fetched],
        *,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Promote a loose Memory to tethered, making it Searchable.

        Tethering an already-tethered Memory conflicts. Tethering an absent or
        deleted Memory raises."""
        with self.tracer.start_as_current_span(
            "MemoryService.tether",
            attributes={
                "memory.id": str(memory.id),
                "memory.observed_version": memory.version,
            },
        ) as span:
            _debug(
                logger,
                "Tethering Memory",
                memory_id=str(memory.id),
                observed_version=memory.version,
            )

            async def _tether(tx: Transaction) -> Memory[Fetched]:
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
                        span.set_attribute("memory.conflict_reason", "already_tethered")
                        _debug(
                            logger,
                            "Memory tether conflict",
                            memory_id=str(memory.id),
                            reason="already_tethered",
                            observed_version=memory.version,
                            current_version=fresh_memory.version,
                        )
                        msg = f"Memory {memory.id} is already tethered"
                        raise MemoryConflictError(msg)
                    if fresh_memory.version != memory.version:
                        span.set_attribute("memory.conflict_reason", "stale_version")
                        _debug(
                            logger,
                            "Memory tether conflict",
                            memory_id=str(memory.id),
                            reason="stale_version",
                            observed_version=memory.version,
                            current_version=fresh_memory.version,
                        )
                        msg = f"Tried to update memory {memory.id} with version {memory.version} but had version {fresh_memory.version}"
                        raise MemoryConflictError(msg)
                return fresh_memory

            async with self.database.transaction(mode="immediate") as tx:
                fresh_memory = await _tether(tx)
            await self._try_set_projection(fresh_memory, logger=logger)
            await self._try_index(fresh_memory, logger=logger)
            span.set_attribute("memory.version", fresh_memory.version)
            _info(
                logger,
                "Memory tethered",
                memory_id=str(fresh_memory.id),
                previous_version=memory.version,
                version=fresh_memory.version,
            )
            await self.event_publisher.publish(
                InvalidateEvent(keys=["memories", "review-queue"])
            )
            return fresh_memory

    async def edit_content(
        self,
        memory: Memory[Fetched],
        content: str,
        *,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Edit a Memory's content and bump `updated_at`.

        Authorship gates trust: a human edit *is* the review, so a
        tethered Memory stays tethered (its projection refreshes) and a loose one
        stays loose. Editing an absent or deleted Memory raises.

        `facets`, when supplied, replaces the stored Commons facet set verbatim
        (an empty dict clears it). `None` (the default) leaves facets unchanged
        — the same "omit means don't touch" convention `provenance` uses.
        """
        normalised_content = _normalise_content(content)
        _debug(
            logger,
            "Editing Memory content",
            memory_id=str(memory.id),
            observed_version=memory.version,
            content_length=len(normalised_content),
        )

        async def _edit_content(tx: Transaction) -> Memory[Fetched]:
            edit_query = (
                update(Memory)
                .set(Memory.content.to(normalised_content))
                .set(Memory.updated_at.to(CurrentTimestamp))
                .set(Memory.version.to(memory.version + 1))
            )
            if facets is not None:
                edit_query = edit_query.set(Memory.facets.to(facets))
            matched_rows = await tx.execute(
                edit_query.where(Memory.id.eq(memory.id))
                .where(Memory.deleted_at.is_null())
                .where(Memory.version.eq(memory.version))
            )
            fresh_memory = await self._fetch_active(tx, memory.id)
            if matched_rows == 0:
                # Earlier, we fetched an active Memory. If we're here, it's
                # because the version was stale.
                _debug(
                    logger,
                    "Memory edit conflict",
                    memory_id=str(memory.id),
                    reason="stale_version",
                    observed_version=memory.version,
                    current_version=fresh_memory.version,
                )
                msg = f"Tried to edit memory {memory.id} with version {memory.version} but had version {fresh_memory.version}"
                raise MemoryConflictError(msg)
            return fresh_memory

        async with self.database.transaction(mode="immediate") as tx:
            fresh_memory = await _edit_content(tx)

        # An invariant is that loose memories don't have projections, and loose
        # memories aren't indexed — so both derived artifacts refresh only when
        # the edited Memory is tethered.
        if fresh_memory.tethered_at is not None:
            await self._try_set_projection(fresh_memory, logger=logger)
            await self._try_index(fresh_memory, logger=logger)
        _info(
            logger,
            "Memory content edited",
            memory_id=str(fresh_memory.id),
            previous_version=memory.version,
            version=fresh_memory.version,
            tethered=fresh_memory.tethered_at is not None,
        )
        await self.event_publisher.publish(
            InvalidateEvent(keys=["memories", "review-queue"])
        )
        return fresh_memory

    async def append_content(
        self,
        memory: Memory[Fetched],
        content: str,
        *,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Append agent-routed verbatim content to a live Memory.

        Placement is content-preserving: the existing Memory text remains intact
        and the new human-authored words are added as a timestamped block marked
        as agent-routed. A tethered Memory refreshes its projection and index;
        a loose Memory remains loose and stays out of both.
        """
        normalised_content = _normalise_content(content)
        appended_at = datetime.now(UTC)
        _debug(
            logger,
            "Appending Memory content",
            memory_id=str(memory.id),
            observed_version=memory.version,
            content_length=len(normalised_content),
        )

        async def _append_content(tx: Transaction) -> Memory[Fetched]:
            current_memory = await self._fetch_active(tx, memory.id)
            if current_memory.version != memory.version:
                _debug(
                    logger,
                    "Memory append conflict",
                    memory_id=str(memory.id),
                    reason="stale_version",
                    observed_version=memory.version,
                    current_version=current_memory.version,
                )
                msg = f"Tried to append memory {memory.id} with version {memory.version} but had version {current_memory.version}"
                raise MemoryConflictError(msg)
            _ = await tx.execute(
                update(Memory)
                .set(
                    Memory.content.to(
                        current_memory.content
                        + _agent_routed_append_block(normalised_content, appended_at)
                    )
                )
                .set(Memory.updated_at.to(appended_at))
                .set(Memory.version.to(memory.version + 1))
                .where(Memory.id.eq(memory.id))
                .where(Memory.deleted_at.is_null())
                .where(Memory.version.eq(memory.version))
            )
            return await self._fetch_active(tx, memory.id)

        async with self.database.transaction(mode="immediate") as tx:
            fresh_memory = await _append_content(tx)
        if fresh_memory.tethered_at is not None:
            await self._try_set_projection(fresh_memory, logger=logger)
            await self._try_index(fresh_memory, logger=logger)
        _info(
            logger,
            "Memory content appended",
            memory_id=str(fresh_memory.id),
            previous_version=memory.version,
            version=fresh_memory.version,
            tethered=fresh_memory.tethered_at is not None,
        )
        await self.event_publisher.publish(
            InvalidateEvent(keys=["memories", "review-queue"])
        )
        return fresh_memory

    async def fetch_active(
        self,
        memory_id: UUID7,
        *,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Fetch a live Memory by id for capability-level policy checks."""
        _debug(logger, "Fetching active Memory", memory_id=str(memory_id))
        async with self.database.transaction() as tx:
            return await self._fetch_active(tx, memory_id)

    async def delete(
        self,
        memory: Memory[Fetched],
        *,
        logger: Logger,
    ) -> Memory[Fetched]:
        """Reject a Memory by soft-deleting it: stamp `deleted_at`, retain the row.

        All deletions are soft regardless of state, so a rejected Memory stays
        recoverable in the DB while dropping out of every queue, the assistant's
        Search, and the KB. Deleting an absent or already-deleted Memory raises.
        """
        _debug(
            logger,
            "Deleting Memory",
            memory_id=str(memory.id),
            observed_version=memory.version,
        )

        async def _delete(tx: Transaction) -> Memory[Fetched]:
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
                _debug(
                    logger,
                    "Memory delete conflict",
                    memory_id=str(memory.id),
                    reason="already_deleted_or_stale_version",
                    observed_version=memory.version,
                    current_version=deleted_memory.version,
                )
                msg = f"Memory {memory.id} is already deleted"
                raise MemoryConflictError(msg)
            return deleted_memory

        async with self.database.transaction(mode="immediate") as tx:
            deleted_memory = await _delete(tx)
        await self._try_remove_projection(memory.id, logger=logger)
        await self._try_deindex(deleted_memory.id, logger=logger)
        _info(
            logger,
            "Memory deleted",
            memory_id=str(deleted_memory.id),
            previous_version=memory.version,
            version=deleted_memory.version,
            was_tethered=deleted_memory.tethered_at is not None,
        )
        await self.event_publisher.publish(
            InvalidateEvent(keys=["memories", "review-queue"])
        )
        return deleted_memory

    async def regenerate_knowledge_base(
        self,
        *,
        logger: Logger,
    ) -> None:
        """Rebuild the Knowledge base projection from live SQLite state.

        This is the recovery path for any post-commit projection write that
        failed during a mutation: SQLite remains the source of truth, and the
        markdown projection can be derived again.
        """
        _debug(logger, "Regenerating Knowledge base")
        async with self.database.transaction() as tx:
            tethered_memories = await tx.fetch_all(tethered_corpus())
        expected_filenames = {f"{memory.id}.md" for memory in tethered_memories}
        projection_root = AsyncPath(memory_projection_root(self.kb_service.kb_root))
        removed_count = 0
        if await projection_root.exists():
            async for path in projection_root.iterdir():
                if path.suffix != ".md" or path.name in expected_filenames:
                    continue
                try:
                    content = await path.read_text(encoding="utf-8")
                except OSError, UnicodeError:
                    continue
                if not is_managed_projection(filename=path.name, content=content):
                    continue
                _debug(
                    logger,
                    "Removing stale projection",
                    projection_path=str(path),
                )
                await path.unlink()
                removed_count += 1
        for memory in tethered_memories:
            _debug(
                logger, "Writing Knowledge base projection", memory_id=str(memory.id)
            )
            await self.kb_service.set_projection(memory)
        _info(
            logger,
            "Knowledge base regenerated",
            projected_count=len(tethered_memories),
            removed_count=removed_count,
        )

    async def facet_overview(self, *, logger: Logger) -> list[FacetOverviewEntry]:
        """Report distinct Commons facet keys/values and how many Memories carry each.

        The facet-curation read side is computed with SQLite's `json_each` over
        the `facets` column, grouped by
        `(key, value)`. Scoped to non-deleted Memories — both loose and tethered
        — because facet drift can exist before a Memory is ever tethered, and
        curation should be able to see (and later fix, via `rename_facet_key` /
        `merge_facet_value`) drift on the whole live corpus, not just the
        assistant-visible tethered slice. A soft-deleted Memory's facets are
        excluded: it is no longer part of the corpus curation is about.
        """
        _debug(logger, "Computing facet overview")
        overview_sql = (
            "SELECT je.key, je.value, COUNT(*) "
            'FROM "memory", json_each("memory"."facets") AS je '
            'WHERE "memory"."deleted_at" IS NULL '
            "GROUP BY je.key, je.value "
            "ORDER BY je.key, je.value"
        )
        async with self.database.transaction() as tx:
            connection = tx.require_connection()
            cursor = await connection.execute(overview_sql, ())
            rows = await cursor.fetchall()
            await cursor.close()
        entries = [
            FacetOverviewEntry(
                key=str(row[0]),
                value=str(row[1]),
                count=cast("int", row[2]),
            )
            for row in rows
        ]
        _debug(logger, "Facet overview computed", entry_count=len(entries))
        return entries

    async def rename_facet_key(
        self,
        old_key: str,
        new_key: str,
        *,
        logger: Logger,
    ) -> int:
        """Rename a Commons facet key across every non-deleted Memory that carries it.

        The calling tool surface must obtain explicit user approval in chat
        *before* invoking this. That requirement lives in the tool description,
        not in this method, so direct service callers must honor it too. Only
        rows that actually carry `old_key` are touched; each touched row's
        `version`/`updated_at` bumps exactly once, and a tethered row's KB
        projection refreshes immediately so `regenerate_knowledge_base` is not
        the only path that picks up the change. Returns the count of rows
        changed.
        """

        async def _rename(tx: Transaction) -> list[Memory[Fetched]]:
            rows = await tx.fetch_all(select(Memory).where(Memory.deleted_at.is_null()))
            changed: list[Memory[Fetched]] = []
            for row in rows:
                if old_key not in row.facets:
                    continue
                renamed_facets = dict(row.facets)
                renamed_facets[new_key] = renamed_facets.pop(old_key)
                matched_rows = await tx.execute(
                    update(Memory)
                    .set(Memory.facets.to(renamed_facets))
                    .set(Memory.version.to(row.version + 1))
                    .set(Memory.updated_at.to(CurrentTimestamp))
                    .where(Memory.id.eq(row.id))
                    .where(Memory.version.eq(row.version))
                )
                if matched_rows:
                    changed.append(await self._fetch_active(tx, row.id))
            return changed

        async with self.database.transaction(mode="immediate") as tx:
            changed_memories = await _rename(tx)
        for memory in changed_memories:
            if memory.tethered_at is not None:
                await self._try_set_projection(memory, logger=logger)
        _info(
            logger,
            "Facet key renamed",
            old_key=old_key,
            new_key=new_key,
            changed_count=len(changed_memories),
        )
        if changed_memories:
            await self.event_publisher.publish(
                InvalidateEvent(keys=["memories", "review-queue"])
            )
        return len(changed_memories)

    async def merge_facet_value(
        self,
        key: str,
        old_value: str,
        new_value: str,
        *,
        logger: Logger,
    ) -> int:
        """Rewrite one facet value to another across every Memory carrying it.

        The calling tool surface must obtain explicit user approval in chat
        *before* invoking this. That requirement lives in the tool description,
        not in this method. Only non-deleted rows where
        `facets[key] == old_value` are
        touched; each touched row's `version`/`updated_at` bumps exactly once,
        and a tethered row's KB projection refreshes immediately. Returns the
        count of rows changed.
        """

        async def _merge(tx: Transaction) -> list[Memory[Fetched]]:
            rows = await tx.fetch_all(select(Memory).where(Memory.deleted_at.is_null()))
            changed: list[Memory[Fetched]] = []
            for row in rows:
                if row.facets.get(key) != old_value:
                    continue
                merged_facets = dict(row.facets)
                merged_facets[key] = new_value
                matched_rows = await tx.execute(
                    update(Memory)
                    .set(Memory.facets.to(merged_facets))
                    .set(Memory.version.to(row.version + 1))
                    .set(Memory.updated_at.to(CurrentTimestamp))
                    .where(Memory.id.eq(row.id))
                    .where(Memory.version.eq(row.version))
                )
                if matched_rows:
                    changed.append(await self._fetch_active(tx, row.id))
            return changed

        async with self.database.transaction(mode="immediate") as tx:
            changed_memories = await _merge(tx)
        for memory in changed_memories:
            if memory.tethered_at is not None:
                await self._try_set_projection(memory, logger=logger)
        _info(
            logger,
            "Facet value merged",
            key=key,
            old_value=old_value,
            new_value=new_value,
            changed_count=len(changed_memories),
        )
        if changed_memories:
            await self.event_publisher.publish(
                InvalidateEvent(keys=["memories", "review-queue"])
            )
        return len(changed_memories)

    async def _try_set_projection(
        self,
        memory: Memory[Fetched],
        *,
        logger: Logger,
    ) -> None:
        """Log post-commit projection failures without hiding the DB write."""
        _debug(logger, "Writing Memory projection", memory_id=str(memory.id))
        try:
            await self.kb_service.set_projection(memory)
        except Exception:
            _exception(logger, "Failed to project Memory", memory_id=str(memory.id))

    async def _try_remove_projection(
        self,
        memory_id: UUID7,
        *,
        logger: Logger,
    ) -> None:
        """Log post-commit projection removal failures after soft-delete."""
        _debug(logger, "Removing Memory projection", memory_id=str(memory_id))
        try:
            await self.kb_service.remove_projection(memory_id)
        except Exception:
            _exception(
                logger,
                "Failed to remove Memory projection",
                memory_id=str(memory_id),
            )

    async def _try_index(
        self,
        memory: Memory[Fetched],
        *,
        logger: Logger,
    ) -> None:
        """Best-effort index a Memory after a tether/edit; never fails the write.

        Like the markdown projection, the index entry is a derived artifact and
        SQLite is canonical: a failed hook is logged, not raised, because the
        reconciler's pass is the correctness backstop. No-op when search is
        unwired."""
        if self.indexer is None:
            return
        _debug(logger, "Indexing Memory for search", memory_id=str(memory.id))
        try:
            await self.indexer.index_memory(memory, logger=logger)
        except Exception:
            _exception(
                logger, "Failed to index Memory for search", memory_id=str(memory.id)
            )

    async def _try_deindex(
        self,
        memory_id: UUID7,
        *,
        logger: Logger,
    ) -> None:
        """Best-effort drop a Memory from the index after delete; never raises."""
        if self.indexer is None:
            return
        _debug(logger, "Deindexing Memory from search", memory_id=str(memory_id))
        try:
            await self.indexer.deindex_memory(memory_id, logger=logger)
        except Exception:
            _exception(
                logger,
                "Failed to deindex Memory from search",
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
