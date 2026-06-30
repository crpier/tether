"""Behavior tests for retrying transient SQLite write-lock contention.

`run_in_transaction` wraps `Database.transaction()` so a body that fails with
the specific "database is locked" shape snekql raises is retried with
backoff; anything else propagates immediately. These tests drive the retry
control flow directly against a real in-memory `Database` — the transaction
boundary is what's under test, not any particular table.
"""

import sqlite3
from collections.abc import AsyncGenerator

from snekql.sqlite import (
    Config,
    Database,
    DatabaseRuntimeError,
    ExecutionError,
    Transaction,
)
from snektest import assert_eq, assert_raises, fixture, load_fixture, test

from tether.db_retry import run_in_transaction


class StubMemoryError(MemoryError):
    """A non-database error a transaction body might raise, for contrast."""


def _locked_database_error() -> ExecutionError:
    """Build the exact exception shape snekql raises for SQLite lock contention."""
    error = ExecutionError("write failed", sql="INSERT", params=())
    error.__cause__ = sqlite3.OperationalError("database is locked")
    return error


def _other_operational_error() -> ExecutionError:
    """A database failure that is not lock contention, so it must not retry."""
    error = ExecutionError("write failed", sql="INSERT", params=())
    error.__cause__ = sqlite3.OperationalError("no such table: widgets")
    return error


@fixture
async def database() -> AsyncGenerator[Database]:
    """A bare in-memory database, just enough to open real transactions on."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    yield db
    await db.close()


@test()
async def a_body_that_succeeds_runs_exactly_once() -> None:
    """No failure means no retry overhead."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        return "settled"

    result = await run_in_transaction(db, _body)

    assert_eq(result, "settled")
    assert_eq(len(attempts), 1)


@test()
async def lock_contention_is_retried_until_it_succeeds() -> None:
    """Transient 'database is locked' failures are retried with backoff."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        if len(attempts) < 3:
            raise _locked_database_error()
        return "settled"

    result = await run_in_transaction(db, _body)

    assert_eq(result, "settled")
    assert_eq(len(attempts), 3)


@test()
async def persistent_lock_contention_eventually_raises() -> None:
    """Retries are bounded: a permanently locked database still fails."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        raise _locked_database_error()

    with assert_raises(ExecutionError):
        _ = await run_in_transaction(db, _body)
    assert_eq(len(attempts) > 1, True)


@test()
async def non_lock_database_errors_are_not_retried() -> None:
    """A database failure unrelated to lock contention propagates immediately."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        raise _other_operational_error()

    with assert_raises(ExecutionError):
        _ = await run_in_transaction(db, _body)
    assert_eq(len(attempts), 1)


@test()
async def non_database_errors_are_not_retried() -> None:
    """Domain errors a body raises on purpose (e.g. not-found) propagate as-is."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        raise StubMemoryError("not a database error")

    with assert_raises(StubMemoryError):
        _ = await run_in_transaction(db, _body)
    assert_eq(len(attempts), 1)


@test()
async def lock_contention_predicate_ignores_unrelated_database_runtime_errors() -> None:
    """A DatabaseRuntimeError that isn't lock contention isn't mistaken for it."""
    db = await load_fixture(database())
    attempts: list[None] = []

    async def _body(tx: Transaction) -> str:
        del tx
        attempts.append(None)
        msg = "transaction is closed"
        raise DatabaseRuntimeError(msg)

    with assert_raises(DatabaseRuntimeError):
        _ = await run_in_transaction(db, _body)
    assert_eq(len(attempts), 1)
