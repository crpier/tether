"""Artifact service layer: freeform agent-generated HTML pages + their events.

An Artifact is an immutable, versioned document the agent authors (a form, a
small game, a quiz, a Lesson) — the second half of the presentation split ADR
0011 names alongside the closed Widget vocabulary. `artifact_id` is stable
across a document's whole history; each edit appends a new row at
`version + 1` rather than mutating one in place, so "latest" is a query
(`max(version)` for an id), never a flag, and every past version stays
fetchable.

An ArtifactEvent is an append-only row scoped to one `artifact_id`: the sole
talk-back channel from a rendered artifact (a quiz answer, a form submission),
relayed by the browser from the sandboxed iframe's `postMessage`. Events carry
opaque, free-form JSON — no schema, matching the Commons facet philosophy —
and are never updated or deleted, so the log is a durable audit trail.

>>> service = ArtifactService(database=database, tracer=tracer)
>>> artifact = await service.create("Quiz", "<html></html>", logger=logger)
>>> artifact.version
1
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, Json, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)

ARTIFACT_HTML_SIZE_CAP_BYTES = 1_000_000
"""Host-side cap on an Artifact's `html` payload (~1 MB), UTF-8 encoded."""


class ArtifactNotFoundError(Exception):
    """Raised when an operation targets an artifact id (or id+version) that
    does not exist."""


class ArtifactHtmlTooLargeError(Exception):
    """Raised when `html` exceeds `ARTIFACT_HTML_SIZE_CAP_BYTES` on Create/Update."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


def _check_html_size(html: str) -> None:
    """Reject an oversized `html` payload with a clear domain error."""
    size = len(html.encode("utf-8"))
    if size > ARTIFACT_HTML_SIZE_CAP_BYTES:
        msg = (
            f"artifact html is {size} bytes, exceeding the "
            f"{ARTIFACT_HTML_SIZE_CAP_BYTES}-byte cap"
        )
        raise ArtifactHtmlTooLargeError(msg)


class Artifact[S = Pending](Model[S, "Artifact[Fetched]"]):
    id: Artifact.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    """This row's own id — one per version, never reused."""
    artifact_id: Artifact.Col[UUID7] = Text()
    """The stable identity across every version of this document."""
    version: Artifact.Col[PositiveInt] = Integer()
    """1-based, incrementing by exactly 1 per Update; immutable once written."""
    title: Artifact.Col[str] = Text()
    html: Artifact.Col[str] = Text()
    created_at: Artifact.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(artifact_id, version)]


class ArtifactEvent[S = Pending](Model[S, "ArtifactEvent[Fetched]"]):
    id: ArtifactEvent.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    artifact_id: ArtifactEvent.Col[UUID7] = Text()
    """The artifact this event was reported by/about; append-only, never mutated."""
    payload: ArtifactEvent.Col[Json[dict[str, JsonValue]]] = Text()
    """Opaque, free-form event data — no schema enforced, by convention an
    optional `type` key names it for whoever renders it later."""
    created_at: ArtifactEvent.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(artifact_id)]


class ArtifactService:
    """Capability surface for Artifacts and their events, over a snekql database.

    Every mutation owns its own transaction (one mutation, one commit) and
    returns the resulting row so the REST and tool layers can render it.
    """

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.tracer: Tracer = tracer

    async def create(
        self,
        title: str,
        html: str,
        *,
        logger: Logger,
    ) -> Artifact[Fetched]:
        """Create a new artifact at version 1 under a freshly minted identity."""
        _check_html_size(html)
        with self.tracer.start_as_current_span("ArtifactService.create") as span:
            _debug(logger, "Creating artifact", title=title)

            async def _create(tx: Transaction) -> Artifact[Fetched]:
                return await tx.execute(
                    insert(
                        Artifact(
                            artifact_id=uuid7(),
                            version=1,
                            title=title,
                            html=html,
                        )
                    ).returning()
                )

            artifact = await run_in_transaction(self.database, _create)
            span.set_attribute("artifact.artifact_id", str(artifact.artifact_id))
            span.set_attribute("artifact.version", artifact.version)
            _info(
                logger,
                "Artifact created",
                artifact_id=str(artifact.artifact_id),
                version=artifact.version,
            )
            await self.event_publisher.publish(InvalidateEvent(keys=["artifacts"]))
            return artifact

    async def update(
        self,
        artifact_id: UUID7,
        html: str,
        *,
        logger: Logger,
    ) -> Artifact[Fetched]:
        """Append a new version of an existing artifact, carrying its title forward.

        Raises `ArtifactNotFoundError` if `artifact_id` names no artifact.
        """
        _check_html_size(html)
        _debug(logger, "Updating artifact", artifact_id=str(artifact_id))

        async def _update(tx: Transaction) -> Artifact[Fetched]:
            latest = await self._fetch_latest(tx, artifact_id)
            return await tx.execute(
                insert(
                    Artifact(
                        artifact_id=artifact_id,
                        version=latest.version + 1,
                        title=latest.title,
                        html=html,
                    )
                ).returning()
            )

        artifact = await run_in_transaction(self.database, _update)
        _info(
            logger,
            "Artifact updated",
            artifact_id=str(artifact.artifact_id),
            version=artifact.version,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["artifacts"]))
        return artifact

    async def get_latest(
        self,
        artifact_id: UUID7,
        *,
        logger: Logger,
    ) -> Artifact[Fetched]:
        """Fetch an artifact's newest version. Raises if the id is unknown."""
        _debug(logger, "Fetching latest artifact version", artifact_id=str(artifact_id))
        async with self.database.transaction() as tx:
            return await self._fetch_latest(tx, artifact_id)

    async def get_version(
        self,
        artifact_id: UUID7,
        version: PositiveInt,
        *,
        logger: Logger,
    ) -> Artifact[Fetched]:
        """Fetch one specific version of an artifact.

        Raises `ArtifactNotFoundError` if the id or the id+version pair is
        unknown.
        """
        _debug(
            logger,
            "Fetching artifact version",
            artifact_id=str(artifact_id),
            version=version,
        )
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(Artifact).where(
                    Artifact.artifact_id.eq(artifact_id) & Artifact.version.eq(version)
                )
            )
        if row is None:
            raise ArtifactNotFoundError(artifact_id)
        return row

    async def list_artifacts(self, *, logger: Logger) -> list[Artifact[Fetched]]:
        """List every artifact's latest version, newest-created-or-updated first."""
        _debug(logger, "Listing artifacts")
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(
                select(Artifact).all().order_by(Artifact.version.desc())
            )
        latest: dict[UUID7, Artifact[Fetched]] = {}
        for row in rows:
            if row.artifact_id not in latest:
                latest[row.artifact_id] = row
        return sorted(latest.values(), key=lambda a: a.created_at, reverse=True)

    async def record_event(
        self,
        artifact_id: UUID7,
        payload: dict[str, JsonValue],
        *,
        logger: Logger,
    ) -> ArtifactEvent[Fetched]:
        """Append one event to an artifact's log. Raises if the id is unknown."""
        _debug(logger, "Recording artifact event", artifact_id=str(artifact_id))

        async def _record(tx: Transaction) -> ArtifactEvent[Fetched]:
            _ = await self._fetch_latest(tx, artifact_id)
            return await tx.execute(
                insert(
                    ArtifactEvent(artifact_id=artifact_id, payload=payload)
                ).returning()
            )

        event = await run_in_transaction(self.database, _record)
        _info(logger, "Artifact event recorded", artifact_id=str(artifact_id))
        await self.event_publisher.publish(InvalidateEvent(keys=["artifacts"]))
        return event

    async def list_events(
        self,
        artifact_id: UUID7,
        *,
        logger: Logger,
    ) -> list[ArtifactEvent[Fetched]]:
        """List an artifact's events, oldest first. Raises if the id is unknown."""
        _debug(logger, "Listing artifact events", artifact_id=str(artifact_id))
        async with self.database.transaction() as tx:
            _ = await self._fetch_latest(tx, artifact_id)
            return await tx.fetch_all(
                select(ArtifactEvent)
                .where(ArtifactEvent.artifact_id.eq(artifact_id))
                .order_by(ArtifactEvent.created_at.asc())
            )

    async def _fetch_latest(
        self, tx: Transaction, artifact_id: UUID7
    ) -> Artifact[Fetched]:
        """Fetch the newest version of an artifact within a transaction, or raise."""
        row = await tx.fetch_one_or_none(
            select(Artifact)
            .where(Artifact.artifact_id.eq(artifact_id))
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        if row is None:
            raise ArtifactNotFoundError(artifact_id)
        return row


async def create_artifact_schema(database: Database) -> None:
    """Create the Artifact and ArtifactEvent tables on an initialized database.

    Applied as its own ordered migrations after the other domains' (prefix
    `011_`). A snekql migration body runs exactly one statement, so scaffolding
    each model's (table, index) pair becomes two ordered migrations apiece.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_artifact_schema(database)
    """
    migrations = {
        f"011_{label}": sql
        for label, sql in (
            *scaffold_sqlite_statements([Artifact]),
            *scaffold_sqlite_statements([ArtifactEvent]),
        )
    }
    await database.migrate(migrations)
