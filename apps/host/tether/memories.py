"""Memory spine service layer: Capture, Review (tether / edit / reject), Search.

A Memory is Captured `loose`, becomes `tethered` once a human vets it, and
is soft-deleted on reject. The load-bearing invariant is that the assistant
Searches only tethered, non-deleted Memories.

>>> service = MemoryService(database=database, kb_root=kb_root)
>>> memory = await service.capture("I prefer aisle seats", logger=logger)
>>> tethered = await service.tether(memory, logger=logger)
>>> [hit.content for hit in await service.search("aisle", logger=logger)]
['I prefer aisle seats']
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast
from uuid import uuid7

from anyio import NamedTemporaryFile, Path
from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel, DirectoryPath, Json, PositiveInt
from snekql.sqlite import (
    Blob,
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    SelectModelQuery,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from yaml import safe_dump, safe_load

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from tether.search_index import SearchCandidate

type MemoryState = Literal["loose", "tethered"]
"""A Memory's trust state. `deleted` is an orthogonal soft-delete marker, not a state."""


class MemoryProvenance(TypedDict):
    """The origin of a Captured Memory.

    `kind` records the source. `confidence` and `batch` are forward-compatible
    optional signals for non-manual producers (import, YouTube, web): a captured
    fact's trustworthiness and the bulk run it arrived in. Manual capture omits
    both, so it still serializes to exactly `{"kind": "manual"}`.
    """

    kind: Literal["manual", "import", "youtube", "web", "readwise"]
    confidence: NotRequired[Literal["low", "medium", "high"]]
    batch: NotRequired[str]


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


class SearchUnavailableError(Exception):
    """Raised when `search` is called on a MemoryService wired without a searcher.

    Search needs the embedder + index seam (the `SearchReconciler`). A bare
    MemoryService (e.g. some tests, the Recall path) never calls `search`, so the
    dependency is optional; reaching `search` without it is a wiring bug."""


class EmptyMemoryContentError(Exception):
    """Raised when Memory content is blank after trimming whitespace."""


class ProjectionStructureError(Exception):
    """Raised when a projection file is not structured as expected."""


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
    facets: Memory.Col[Json[dict[str, str]]] = Text(default_factory=dict[str, str])
    """The Commons facet set: a flat `{"key": "value"}` map, one string value per
    key. Naming convention is lowercase snake_case keys and free-form lowercase
    string values, documented but not validated here — key/value drift is
    handled by curation (`rename_facet_key` / `merge_facet_value`), not code.
    `sensitivity` is a reserved key name but stored and treated like any other
    facet; there is no special-cased code path for it."""
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
    embedding: Memory.Col[bytes | None] = Blob(
        default=None,
        nullable=True,
    )
    """Canonical embedding vector for this Memory, as raw bytes.

    SQLite is the source of truth for the vector; the LanceDB index is a derived
    projection rebuilt from it. `None` until the embedder has run."""
    embedded_version: Memory.Col[int | None] = Integer(
        default=None,
        nullable=True,
    )
    """The content `version` the stored `embedding` reflects.

    `None` means an embedding is owed (never produced, or content changed since).
    The reconciler embeds any Memory whose `embedded_version != version`."""


class FrontMatter(BaseModel):
    id: UUID7
    created_at: datetime
    updated_at: datetime
    provenance: MemoryProvenance
    tethered_at: datetime | None
    facets: dict[str, str] = {}


class FrontMatterConversion:
    # TODO: double-check that this approach makes sense
    @staticmethod
    def generate_frontmatter(memory: Memory[Fetched]) -> str:
        """Generate a string containing the YAML frontmatter for a Memory,
        including its separators.

        `facets` is omitted entirely when the Memory carries none, rather than
        projected as an empty mapping — an empty `facets:` key would read as
        "curated to nothing" instead of "no Commons facets yet"."""
        frontmatter: dict[str, object] = {
            "id": str(memory.id),
            "created_at": memory.created_at,
            "updated_at": memory.updated_at,
            "provenance": memory.provenance,
            "tethered_at": memory.tethered_at,
        }
        if memory.facets:
            frontmatter["facets"] = memory.facets
        yaml_string = safe_dump(frontmatter)
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


class MemorySearcher(Protocol):
    """The search seam the spine needs: the query read-path plus the index hooks.

    A structural Protocol (satisfied by `SearchReconciler`) so the spine does not
    import the concrete reconciler — that import would close a cycle, since the
    reconciler depends on `Memory`."""

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]: ...
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
        kb_service: KnowledgeBaseService,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
        searcher: MemorySearcher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.kb_service: KnowledgeBaseService = kb_service
        self.tracer: Tracer = tracer
        self.searcher: MemorySearcher | None = searcher
        """Search seam (embedder + index + reconciler hooks); `None` if unwired.

        Optional because the Recall path and many service tests construct a bare
        MemoryService that never searches. The Review trigger sites best-effort
        index through it; `search` requires it."""

    @staticmethod
    def tethered_corpus() -> SelectModelQuery[Memory, Memory[Fetched]]:
        """The ADR-0001 trusted corpus: tethered, non-deleted Memories.

        The single home of the trust predicate — corpus selections compose on
        this (chained `.where` accumulates as AND) instead of re-encoding it."""
        return select(Memory).where(
            Memory.tethered_at.is_not_null() & Memory.deleted_at.is_null()
        )

    @staticmethod
    def loose_queue() -> SelectModelQuery[Memory, Memory[Fetched]]:
        """The Review queue: loose (untethered), non-deleted Memories."""
        return select(Memory).where(
            Memory.tethered_at.is_null() & Memory.deleted_at.is_null()
        )

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

            memory = await run_in_transaction(self.database, _capture)
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

            memory = await run_in_transaction(self.database, _capture)
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

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        facets: dict[str, str] | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Hybrid Search the assistant uses to pull context.

        The query is embedded and run through the index's lexical + semantic
        arms, fused by RRF; the ranked candidate ids are then re-fetched from
        SQLite and re-filtered to `tethered ∧ ¬deleted`. That upstream re-filter
        is where the assistant-only-sees-tethered invariant is enforced: a
        drifted index (an orphan a missed event left behind) can surface a
        candidate, but a loose or deleted Memory is dropped here and never
        reaches the assistant. Results keep the index's
        relevance order; the SQLite round-trip preserves it, not recency.

        `facets`, when supplied, is an exact-match AND filter applied at this
        same re-fetch stage: a Memory must carry every given key with exactly
        that value to survive, composing with (not replacing) the tethered /
        deleted invariant. LanceDB's index is untouched by this — it only ever
        stores id/content/vector — so the filter runs against the re-fetched
        SQLite rows, same as the trust predicate. Omitted or empty `facets`
        preserves the unfiltered-by-facet behavior."""
        normalised_query = query.strip()
        if not normalised_query:
            msg = "keyword Search requires a non-empty query"
            raise EmptySearchQueryError(msg)
        with self.tracer.start_as_current_span(
            "MemoryService.search",
            attributes={"memory.search.limit": limit},
        ) as span:
            _debug(logger, "Searching Memories", limit=limit)
            candidates = await self.search_candidates(
                normalised_query, limit=limit, logger=logger
            )
            span.set_attribute("memory.search.candidate_count", len(candidates))
            if not candidates:
                _debug(
                    logger,
                    "Memory Search completed",
                    limit=limit,
                    candidate_count=0,
                    result_count=0,
                )
                return []
            rank = {
                candidate.id: position for position, candidate in enumerate(candidates)
            }
            memories = await self.hydrate_tethered(
                list(rank), facets=facets, logger=logger
            )
            memories.sort(key=lambda memory: rank[memory.id])
            span.set_attribute("memory.search.result_count", len(memories))
            _debug(
                logger,
                "Memory Search completed",
                limit=limit,
                candidate_count=len(candidates),
                result_count=len(memories),
            )
            return memories

    async def search_candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]:
        """Raw ranked candidate ids from the index, unfiltered by tether/delete state.

        The read half of the fusion seam (`tether.search_fusion`): a caller
        doing its own cross-source ranking needs candidates before the SQLite
        re-filter, whereas `search` does both steps in one call. Assumes
        `query` is already non-empty."""
        if self.searcher is None:
            msg = "MemoryService.search_candidates requires a configured searcher"
            raise SearchUnavailableError(msg)
        return await self.searcher.candidates(query, limit=limit, logger=logger)

    async def hydrate_tethered(
        self,
        ids: Sequence[UUID],
        *,
        facets: Mapping[str, str] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Re-fetch candidate ids from SQLite, filtered to tethered ∧ ¬deleted (+facets/window).

        The shared re-filter step `search` and fusion both need: candidate ids
        from the index carry no guarantee they're still valid rows, so this is
        where the assistant-only-sees-tethered invariant (ADR 0001) and ADR
        0009's per-arm re-filter both land. `after`/`before`, when supplied,
        bound `tethered_at` (inclusive) the same way a Memory is timestamped
        for Search — a hard SQLite-stage filter, applied alongside the
        tethered/deleted predicate rather than as a rank signal. Like `facets`,
        a narrow window can shrink the hydrated set below the candidate count
        the index returned; callers do not re-fetch to compensate, matching
        the existing facet-filter behavior. Result order is not preserved —
        callers sort by their own candidate ranking."""
        _debug(logger, "Hydrating tethered Memory candidates", candidate_count=len(ids))
        if not ids:
            return []
        query = MemoryService.tethered_corpus().where(Memory.id.in_(*ids))
        if after is not None:
            query = query.where(Memory.tethered_at.gte(after))
        if before is not None:
            query = query.where(Memory.tethered_at.lte(before))
        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(query)
        if facets:
            memories = [
                memory
                for memory in memories
                if all(memory.facets.get(key) == value for key, value in facets.items())
            ]
        return memories

    async def browse_by_state(
        self,
        state: MemoryState,
        *,
        limit: int | None = None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """Filter-only Search backing the human review UI (`GET /memories?state=`).

        Human-facing, not assistant-facing: unlike keyword `search` it is not
        bound by the tethered-only invariant — `loose` deliberately returns the
        review queue. Soft-deleted Memories are always excluded. `loose` is
        newest-first (fresh captures reviewed while context is warm); `tethered`
        is ordered by `tethered_at` desc (most recently trusted first). `limit`
        caps the rows returned (`None` is unbounded for the review UI; the
        assistant-facing tool passes a bound to protect the model's context)."""
        _debug(logger, "Browsing Memories by state", state=state)
        match state:
            case "loose":
                browse = MemoryService.loose_queue().order_by(Memory.created_at.desc())
            case "tethered":
                browse = MemoryService.tethered_corpus().order_by(
                    Memory.tethered_at.desc()
                )
        if limit is not None:
            browse = browse.limit(limit)
        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(browse)
        _debug(
            logger,
            "Memory browse completed",
            state=state,
            result_count=len(memories),
        )
        return memories

    # TODO: re-evaluate whether passing primitives instead of objects around
    # was the right call
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

            fresh_memory = await run_in_transaction(self.database, _tether)
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

        fresh_memory = await run_in_transaction(self.database, _edit_content)

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

        deleted_memory = await run_in_transaction(self.database, _delete)
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
            tethered_memories = await tx.fetch_all(MemoryService.tethered_corpus())
        expected_filenames = {f"{memory.id}.md" for memory in tethered_memories}
        removed_count = 0
        async for path in Path(self.kb_service.kb_root).iterdir():
            if path.suffix == ".md" and path.name not in expected_filenames:
                _debug(logger, "Removing stale projection", projection_path=str(path))
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

        The Proposal-lite curation surface's read side (issue #185): computed
        with SQLite's `json_each` over the `facets` column, grouped by
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

        Bulk curation (issue #185, "Proposal-lite"): the calling tool surface
        must have obtained explicit user approval in chat *before* invoking
        this — that requirement lives in the tool description, not in this
        method, so any caller of the service directly must honor it too. Only
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

        changed_memories = await run_in_transaction(self.database, _rename)
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

        Bulk curation (issue #185, "Proposal-lite"): the calling tool surface
        must have obtained explicit user approval in chat *before* invoking
        this — that requirement lives in the tool description, not in this
        method. Only non-deleted rows where `facets[key] == old_value` are
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

        changed_memories = await run_in_transaction(self.database, _merge)
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
        if self.searcher is None:
            return
        _debug(logger, "Indexing Memory for search", memory_id=str(memory.id))
        try:
            await self.searcher.index_memory(memory, logger=logger)
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
        if self.searcher is None:
            return
        _debug(logger, "Deindexing Memory from search", memory_id=str(memory_id))
        try:
            await self.searcher.deindex_memory(memory_id, logger=logger)
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


# snekql builds schema by replaying a hand-authored migration chain and records
# each step by *name*, never re-running or checksumming an applied one. So a
# migration body must be frozen at authoring time: editing `001_memories` to add
# columns (e.g. via `scaffold([Memory])`, which regenerates the current model)
# adds them to fresh databases but silently skips every existing one. New columns
# therefore arrive as their own forward migration, applied on top of the frozen
# base. Replaying the whole chain on a fresh database yields the current schema.
_MEMORY_MIGRATIONS: dict[str, str] = {
    # Original Memory table, as first shipped (before embedding columns). Frozen.
    "001_memories": (
        'CREATE TABLE "memory" ('
        '"id" TEXT PRIMARY KEY, '
        '"content" TEXT, '
        '"version" INTEGER, '
        '"provenance" TEXT, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"tethered_at" TEXT, '
        '"deleted_at" TEXT'
        ") STRICT"
    ),
    # Hybrid search (PR #72): canonical embedding vector + the content `version`
    # it reflects. Added here so pre-#72 databases gain the columns on next boot.
    "002_memory_embedding": 'ALTER TABLE "memory" ADD COLUMN "embedding" BLOB',
    "003_memory_embedded_version": (
        'ALTER TABLE "memory" ADD COLUMN "embedded_version" INTEGER'
    ),
    # Commons facets (issue #185): a flat JSON `{"key": "value"}` map. Existing
    # rows backfill to '{}' via the column default.
    "004_memory_facets": (
        'ALTER TABLE "memory" ADD COLUMN "facets" TEXT NOT NULL DEFAULT \'{}\''
    ),
}


async def create_memory_schema(database: Database) -> None:
    """Bring the Memory table to the current schema on an initialized database.

    Applies the frozen Memory migration chain: a fresh database is built by
    replaying every step, while an existing one applies only the steps it has
    not yet seen (so a pre-embedding database gains the embedding columns). The
    caller owns `Database.initialize` (and thus the backend choice) and hands
    the live database here before serving requests.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_memory_schema(database)
    """
    await database.migrate(_MEMORY_MIGRATIONS)
