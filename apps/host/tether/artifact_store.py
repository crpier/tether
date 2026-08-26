"""SQLite models and historical schema chain for Artifacts and their events."""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid7

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
    UtcDatetime,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.artifact_model import JsonValue


class Artifact[S = Pending](Model[S, "Artifact[Fetched]"]):
    id: Artifact.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    """This row's own id — one per version, never reused."""
    artifact_id: Artifact.Col[UUID7] = Text()
    """The stable identity across every version of this document."""
    version: Artifact.Col[PositiveInt] = Integer()
    """1-based, incrementing by exactly 1 per Update; immutable once written."""
    title: Artifact.Col[str] = Text()
    html: Artifact.Col[str] = Text()
    created_at: Artifact.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(artifact_id, version)]


class ArtifactEvent[S = Pending](Model[S, "ArtifactEvent[Fetched]"]):
    id: ArtifactEvent.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    artifact_id: ArtifactEvent.Col[UUID7] = Text()
    """The artifact this event was reported by/about; append-only, never mutated."""
    payload: ArtifactEvent.Col[Json[dict[str, JsonValue]]] = Text()
    """Opaque, free-form event data — no schema enforced, by convention an
    optional `type` key names it for whoever renders it later."""
    created_at: ArtifactEvent.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(artifact_id)]


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
