"""Health Connect SQLite schema and migration compatibility tests."""

import hashlib
import json

from snekql.sqlite import Config, Database
from snektest import assert_eq, test

from tether.health_connect.persistence import (
    create_health_connect_schema,
    health_connect_migrations,
)


@test()
def historical_migration_chain_is_byte_stable() -> None:
    """Refactors cannot alter any key or statement already applied in SQLite."""
    encoded = json.dumps(health_connect_migrations(), separators=(",", ":")).encode()

    assert_eq(
        hashlib.sha256(encoded).hexdigest(),
        "d0ae944a44cb4655270229ebab9b5a5397a990b39a9933b581858abdd94bb4f0",
    )


@test()
async def schema_upgrade_preserves_legacy_positional_migrations() -> None:
    """Adding generic DDL must not shift and replay existing view migrations."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    migrations = health_connect_migrations()
    failed_boot_prefix = dict(list(migrations.items())[:44])
    assert_eq(len(failed_boot_prefix), 44)
    assert_eq(list(failed_boot_prefix)[-1], "0040_health_connect_schema")
    await database.migrate(failed_boot_prefix)

    await create_health_connect_schema(database)
    await create_health_connect_schema(database)

    await database.close()
