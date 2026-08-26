"""Behavior test for canonical Artifact persistence."""

from uuid import uuid7

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, test

from tether.artifact_store import Artifact, create_artifact_schema


@test()
async def artifact_versions_persist_as_separate_rows() -> None:
    """Appending a version retains the prior row under one stable identity."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_artifact_schema(database)
    artifact_id = uuid7()
    async with database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            insert(
                Artifact(
                    artifact_id=artifact_id,
                    html="<p>first</p>",
                    title="Page",
                    version=1,
                )
            )
        )
        _ = await transaction.execute(
            insert(
                Artifact(
                    artifact_id=artifact_id,
                    html="<p>second</p>",
                    title="Page",
                    version=2,
                )
            )
        )
    async with database.transaction() as transaction:
        versions = await transaction.fetch_all(
            select(Artifact)
            .where(Artifact.artifact_id.eq(artifact_id))
            .order_by(Artifact.version.asc())
        )

    assert_eq([version.html for version in versions], ["<p>first</p>", "<p>second</p>"])
    await database.close()
