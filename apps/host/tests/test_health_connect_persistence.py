"""Health Connect SQLite schema and migration compatibility tests."""

import hashlib
import json

from snekql.sqlite import Config, Database, scaffold
from snektest import assert_eq, test

from tether.health_connect.persistence import (
    _CURRENT_VIEW_MIGRATIONS,
    _SCHEMA_MODELS,
    HcGenericRecord,
    create_health_connect_schema,
    health_connect_migrations,
)


@test()
def historical_migration_chain_is_byte_stable() -> None:
    """Refactors cannot alter any key or statement already applied in SQLite."""
    encoded = json.dumps(
        health_connect_migrations(), sort_keys=True, separators=(",", ":")
    ).encode()

    assert_eq(
        hashlib.sha256(encoded).hexdigest(),
        "d05e0266f15d2aeaf80331d10041f551089e30607c92dc17e554a72d34016f16",
    )


@test()
async def schema_upgrade_preserves_legacy_positional_migrations() -> None:
    """Adding generic DDL must not shift and replay existing view migrations."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    legacy_models = [model for model in _SCHEMA_MODELS if model is not HcGenericRecord]
    migrations = {
        f"{index:04d}_health_connect_schema": sql
        for index, sql in enumerate(scaffold(legacy_models).splitlines(), start=1)
    }
    next_index = len(migrations) + 1
    for view, sql in _CURRENT_VIEW_MIGRATIONS.items():
        if view == "hc_generic_record_current":
            continue
        migrations[f"{next_index:04d}_{view}"] = sql
        next_index += 1
    await database.migrate(migrations)
    generic_start = len(scaffold(legacy_models).splitlines()) + 1
    await database.migrate(
        {
            f"{index:04d}_health_connect_schema": sql
            for index, sql in enumerate(
                scaffold([HcGenericRecord]).splitlines(), start=generic_start
            )
        }
    )

    await create_health_connect_schema(database)
    await create_health_connect_schema(database)

    await database.close()
