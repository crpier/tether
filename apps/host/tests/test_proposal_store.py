"""Behavior tests for canonical Proposal persistence and schema upgrades."""

from collections.abc import AsyncGenerator
from uuid import uuid7

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, assert_is_none, fixture, load_fixture, test

from tether.proposal_store import (
    Proposal,
    ProposalAction,
    create_proposal_schema,
)

_LEGACY_PROPOSAL_MIGRATIONS = {
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


@fixture
async def legacy_proposal_database() -> AsyncGenerator[Database]:
    """Create a Proposal database that predates action display text."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate(_LEGACY_PROPOSAL_MIGRATIONS)
    yield database
    await database.close()


@test()
async def fresh_schema_persists_the_current_proposal_shape() -> None:
    """Fresh databases support Proposal actions with display text."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_proposal_schema(database)
    async with database.transaction(mode="immediate") as transaction:
        proposal = await transaction.execute(
            insert(
                Proposal(
                    consumer="test",
                    state="pending",
                    summary="archive it",
                    title="Archive message",
                )
            ).returning()
        )
        action = await transaction.execute(
            insert(
                ProposalAction(
                    display="Archive · Newsletter",
                    disposition="approved",
                    kind="gmail.archive",
                    params_json="{}",
                    proposal_id=str(proposal.id),
                    seq=0,
                )
            ).returning()
        )

    assert_eq(action.display, "Archive · Newsletter")
    await database.close()


@test()
async def upgrading_a_legacy_database_adds_nullable_action_display() -> None:
    """Forward migration adds display without rewriting existing actions."""
    database = await load_fixture(legacy_proposal_database())
    action_id = uuid7()
    async with database.transaction(mode="immediate") as transaction:
        proposal = await transaction.execute(
            insert(
                Proposal(
                    consumer="test",
                    state="pending",
                    summary="archive it",
                    title="Archive message",
                )
            ).returning()
        )
        connection = transaction.require_connection()
        _ = await connection.execute(
            "".join(
                (
                    'INSERT INTO "proposal_action" ',
                    "(id, proposal_id, seq, kind, params_json, disposition) ",
                    "VALUES (?, ?, ?, ?, ?, ?)",
                )
            ),
            (str(action_id), str(proposal.id), 0, "gmail.archive", "{}", "approved"),
        )

    await create_proposal_schema(database)

    async with database.transaction() as transaction:
        action = await transaction.fetch_one(
            select(ProposalAction).where(ProposalAction.id.eq(action_id))
        )
    assert_is_none(action.display)
