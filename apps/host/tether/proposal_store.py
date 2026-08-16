"""Canonical Proposal persistence models and frozen schema migrations."""

from __future__ import annotations

from typing import ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7, PositiveInt
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

type ProposalState = Literal[
    "pending", "approved", "executing", "executed", "failed", "rejected"
]
"""A proposal's lifecycle state; `executing` may be long-lived."""

type ActionDisposition = Literal["approved", "deselected"]
"""Whether an action was kept (`approved`) or unticked before approval."""

type ActionOutcome = Literal["succeeded", "failed", "skipped"]
"""One action's terminal execution result; `skipped` is the fail-soft outcome."""


class Proposal[S = Pending](Model[S, "Proposal[Fetched]"]):
    """An explicitly composed action set, plus its lifecycle state."""

    id: Proposal.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    consumer: Proposal.Col[str] = Text()
    """The registering consumer (e.g. `gmail`) that produced this proposal."""
    title: Proposal.Col[str] = Text()
    summary: Proposal.Col[str] = Text()
    producing_run_id: Proposal.Col[str | None] = Text(default=None, nullable=True)
    """Provenance: the agent run that produced this proposal, when known."""
    state: Proposal.Col[ProposalState] = Text()
    rejection_reason: Proposal.Col[str | None] = Text(default=None, nullable=True)
    version: Proposal.Col[PositiveInt] = Integer(default=1)
    """Optimistic-concurrency version, bumped on every lifecycle transition."""
    created_at: Proposal.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: Proposal.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    decided_at: Proposal.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    """Stamped when the proposal leaves `pending` (approved or rejected)."""

    __indexes__: ClassVar = [Index(state, created_at)]


class ProposalAction[S = Pending](Model[S, "ProposalAction[Fetched]"]):
    """One typed action within a proposal: typed at the seam, JSON at rest."""

    id: ProposalAction.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    proposal_id: ProposalAction.Col[str] = Text()
    """The owning proposal's id (a logical foreign key)."""
    seq: ProposalAction.Col[int] = Integer()
    """Position within the proposal; execution follows ascending `seq`."""
    kind: ProposalAction.Col[str] = Text()
    scope: ProposalAction.Col[str | None] = Text(default=None, nullable=True)
    params_json: ProposalAction.Col[str] = Text()
    """The action's params as JSON; re-validated against the kind at execute."""
    display: ProposalAction.Col[str | None] = Text(default=None, nullable=True)
    """A human-readable one-line summary of this action, supplied by the consumer
    at propose time (e.g. `Archive · "Your order has shipped" · Amazon · Jul 12`).
    NULL for actions composed before this column existed, or by a consumer that
    supplies none; the panel falls back to a kind+params rendering."""
    disposition: ProposalAction.Col[ActionDisposition] = Text()
    """`approved` (kept) or `deselected` (unticked before approval)."""
    outcome: ProposalAction.Col[ActionOutcome | None] = Text(
        default=None, nullable=True
    )
    """Terminal execution result; append-only, never overwritten once set."""
    outcome_detail: ProposalAction.Col[str | None] = Text(default=None, nullable=True)
    executed_at: ProposalAction.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )

    __indexes__: ClassVar = [Index(proposal_id, seq)]


class AutonomyGrant[S = Pending](Model[S, "AutonomyGrant[Fetched]"]):
    """A live trust grant for a `(kind, scope)` category; append-only ledger.

    A bare-kind grant (`scope IS NULL`) covers every scope for that kind. Rows
    are stamped `revoked_at`, never deleted, so a re-grant is a new row and the
    table doubles as a permanent trust-history log.
    """

    id: AutonomyGrant.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    kind: AutonomyGrant.Col[str] = Text()
    scope: AutonomyGrant.Col[str | None] = Text(default=None, nullable=True)
    granted_at: AutonomyGrant.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    revoked_at: AutonomyGrant.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )


def _proposal_migrations() -> dict[str, str]:
    """The ordered Proposal migration chain, one statement per migration.

    The `030_` bodies are the original scaffold (issue #199), frozen verbatim so
    the model classes can keep evolving without rewriting an already-applied
    migration; later shape changes are explicit `ALTER TABLE` steps. Freezing is
    what lets a fresh database and an already-migrated one converge on the same
    schema — a live-scaffold create would fold new columns into `030_`, which an
    existing database has already skipped, so the additive column would never
    land there.
    """
    migrations: dict[str, str] = {
        "030_create_proposal": (
            'CREATE TABLE "proposal" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "consumer" TEXT, "title" TEXT, '
            '"summary" TEXT, "producing_run_id" TEXT, "state" TEXT, '
            '"rejection_reason" TEXT, "version" INTEGER, '
            "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"decided_at" TEXT) STRICT'
        ),
        "030_create_index_ix_proposal_state_created_at": (
            'CREATE INDEX "ix_proposal_state_created_at" '
            'ON "proposal" ("state", "created_at")'
        ),
        "030_create_proposal_action": (
            'CREATE TABLE "proposal_action" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "proposal_id" TEXT, "seq" INTEGER, '
            '"kind" TEXT, "scope" TEXT, "params_json" TEXT, "disposition" TEXT, '
            '"outcome" TEXT, "outcome_detail" TEXT, "executed_at" TEXT) STRICT'
        ),
        "030_create_index_ix_proposal_action_proposal_id_seq": (
            'CREATE INDEX "ix_proposal_action_proposal_id_seq" '
            'ON "proposal_action" ("proposal_id", "seq")'
        ),
        "030_create_autonomy_grant": (
            'CREATE TABLE "autonomy_grant" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "kind" TEXT, "scope" TEXT, '
            "\"granted_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"revoked_at" TEXT) STRICT'
        ),
    }
    # A human-readable per-action display line (issue #199): proposal actions
    # rendered as raw JSON with opaque ids are unreviewable, so the consumer now
    # supplies a display string at propose time. Additive and nullable — rows
    # composed before this column read back NULL and fall back to kind+params.
    migrations["031_proposal_action_display"] = (
        'ALTER TABLE "proposal_action" ADD COLUMN "display" TEXT'
    )
    return migrations


async def create_proposal_schema(database: Database) -> None:
    """Create the proposal tables and their indexes on an initialized database.

    Applied as its own ordered migrations after the earlier schemas: the `030_`
    scaffold bodies are frozen, extended by explicit `031_` column additions. A
    snekql migration body runs exactly one statement, so each table, index, and
    column addition is its own ordered migration.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_proposal_schema(database)
    """
    await database.migrate(_proposal_migrations())
