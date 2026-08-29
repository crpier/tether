"""SQLite models and schema chain for generic Ledgers."""

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

from tether.ledger_model import (
    LedgerFieldDefinition,
    LedgerLifecycleStatus,
    LedgerProposalKind,
    LedgerProposalStatus,
    LedgerScalarValue,
)


class Ledger[S = Pending](Model[S, "Ledger[Fetched]"]):
    """Stable identity shared by every immutable definition revision."""

    id: Ledger.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    created_at: Ledger.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class LedgerRevision[S = Pending](Model[S, "LedgerRevision[Fetched]"]):
    """One approved immutable interpretation of a Ledger."""

    id: LedgerRevision.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    approved_by_conversation_id: LedgerRevision.Col[UUID7] = Text()
    approved_by_message_id: LedgerRevision.Col[UUID7] = Text()
    created_at: LedgerRevision.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    fields: LedgerRevision.Col[Json[list[LedgerFieldDefinition]]] = Text()
    ledger_id: LedgerRevision.Col[UUID7] = Text()
    name: LedgerRevision.Col[str] = Text()
    proposal_id: LedgerRevision.Col[UUID7] = Text()
    purpose: LedgerRevision.Col[str] = Text()
    revision: LedgerRevision.Col[PositiveInt] = Integer()
    status: LedgerRevision.Col[LedgerLifecycleStatus] = Text()

    __indexes__: ClassVar = [Index(ledger_id, revision), Index(proposal_id)]


class LedgerEntry[S = Pending](Model[S, "LedgerEntry[Fetched]"]):
    """One immutable schema-versioned record with exact provenance."""

    id: LedgerEntry.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    dedupe_key: LedgerEntry.Col[str] = Text()
    evidence: LedgerEntry.Col[Json[list[str]]] = Text()
    ledger_id: LedgerEntry.Col[UUID7] = Text()
    occurred_at: LedgerEntry.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    recorded_at: LedgerEntry.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    revision: LedgerEntry.Col[PositiveInt] = Integer()
    source_message_id: LedgerEntry.Col[UUID7] = Text()
    supersedes_entry_id: LedgerEntry.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    values: LedgerEntry.Col[Json[dict[str, LedgerScalarValue]]] = Text()

    __indexes__: ClassVar = [
        Index(ledger_id, recorded_at),
        Index(dedupe_key),
        Index(supersedes_entry_id),
    ]


class LedgerProposal[S = Pending](Model[S, "LedgerProposal[Fetched]"]):
    """One frozen definition awaiting or retaining explicit user approval."""

    id: LedgerProposal.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    approved_at: LedgerProposal.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    approved_by_message_id: LedgerProposal.Col[UUID7 | None] = Text(
        default=None,
        nullable=True,
    )
    base_revision: LedgerProposal.Col[PositiveInt | None] = Integer(
        default=None,
        nullable=True,
    )
    created_at: LedgerProposal.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    fields: LedgerProposal.Col[Json[list[LedgerFieldDefinition]]] = Text()
    kind: LedgerProposal.Col[LedgerProposalKind] = Text()
    ledger_id: LedgerProposal.Col[UUID7] = Text(default_factory=uuid7)
    ledger_status: LedgerProposal.Col[LedgerLifecycleStatus] = Text()
    name: LedgerProposal.Col[str] = Text()
    proposed_by_conversation_id: LedgerProposal.Col[UUID7] = Text()
    proposed_by_message_id: LedgerProposal.Col[UUID7] = Text()
    proposed_revision: LedgerProposal.Col[PositiveInt] = Integer()
    purpose: LedgerProposal.Col[str] = Text()
    status: LedgerProposal.Col[LedgerProposalStatus] = Text()

    __indexes__: ClassVar = [
        Index(status, created_at),
        Index(ledger_id, created_at),
    ]


async def create_ledger_schema(database: Database) -> None:
    """Create generic Ledger storage on an initialized database."""
    await database.migrate(
        {
            "041_create_ledger_proposal": (
                'CREATE TABLE "ledger_proposal" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"approved_at" TEXT, '
                '"approved_by_message_id" TEXT, '
                '"base_revision" INTEGER, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"fields" TEXT NOT NULL, '
                '"kind" TEXT NOT NULL, '
                '"ledger_id" TEXT NOT NULL, '
                '"ledger_status" TEXT NOT NULL, '
                '"name" TEXT NOT NULL, '
                '"proposed_by_conversation_id" TEXT NOT NULL, '
                '"proposed_by_message_id" TEXT NOT NULL, '
                '"proposed_revision" INTEGER NOT NULL, '
                '"purpose" TEXT NOT NULL, '
                '"status" TEXT NOT NULL'
                ") STRICT"
            ),
            "041_create_index_ix_ledger_proposal_status_created_at": (
                'CREATE INDEX "ix_ledger_proposal_status_created_at" '
                'ON "ledger_proposal" ("status", "created_at")'
            ),
            "041_create_index_ix_ledger_proposal_ledger_id_created_at": (
                'CREATE INDEX "ix_ledger_proposal_ledger_id_created_at" '
                'ON "ledger_proposal" ("ledger_id", "created_at")'
            ),
            "041_create_ledger": (
                'CREATE TABLE "ledger" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                ") STRICT"
            ),
            "041_create_ledger_revision": (
                'CREATE TABLE "ledger_revision" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"approved_by_conversation_id" TEXT NOT NULL, '
                '"approved_by_message_id" TEXT NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"fields" TEXT NOT NULL, '
                '"ledger_id" TEXT NOT NULL REFERENCES "ledger" ("id"), '
                '"name" TEXT NOT NULL, '
                '"proposal_id" TEXT NOT NULL REFERENCES "ledger_proposal" ("id"), '
                '"purpose" TEXT NOT NULL, '
                '"revision" INTEGER NOT NULL, '
                '"status" TEXT NOT NULL'
                ") STRICT"
            ),
            "041_create_unique_index_ix_ledger_revision_ledger_revision": (
                'CREATE UNIQUE INDEX "ix_ledger_revision_ledger_revision" '
                'ON "ledger_revision" ("ledger_id", "revision")'
            ),
            "041_create_unique_index_ix_ledger_revision_proposal_id": (
                'CREATE UNIQUE INDEX "ix_ledger_revision_proposal_id" '
                'ON "ledger_revision" ("proposal_id")'
            ),
            "041_create_ledger_entry": (
                'CREATE TABLE "ledger_entry" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"dedupe_key" TEXT NOT NULL, '
                '"evidence" TEXT NOT NULL, '
                '"ledger_id" TEXT NOT NULL REFERENCES "ledger" ("id"), '
                '"occurred_at" TEXT, '
                '"recorded_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"revision" INTEGER NOT NULL, '
                '"source_message_id" TEXT NOT NULL, '
                '"supersedes_entry_id" TEXT REFERENCES "ledger_entry" ("id"), '
                '"values" TEXT NOT NULL'
                ") STRICT"
            ),
            "041_create_index_ix_ledger_entry_ledger_recorded_at": (
                'CREATE INDEX "ix_ledger_entry_ledger_recorded_at" '
                'ON "ledger_entry" ("ledger_id", "recorded_at")'
            ),
            "041_create_unique_index_ix_ledger_entry_dedupe_key": (
                'CREATE UNIQUE INDEX "ix_ledger_entry_dedupe_key" '
                'ON "ledger_entry" ("dedupe_key")'
            ),
            "041_create_unique_index_ix_ledger_entry_supersedes": (
                'CREATE UNIQUE INDEX "ix_ledger_entry_supersedes" '
                'ON "ledger_entry" ("supersedes_entry_id") '
                'WHERE "supersedes_entry_id" IS NOT NULL'
            ),
        }
    )


__all__ = [
    "Ledger",
    "LedgerEntry",
    "LedgerProposal",
    "LedgerRevision",
    "create_ledger_schema",
]
