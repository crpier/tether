"""Retry transient SQLite write-lock contention around snekql transactions.

SQLite's WAL writer lock is global and exclusive: at most one transaction in
this process can be mid-write at a time. snekql already configures
`busy_timeout` so a colliding writer blocks briefly, but once that single
in-driver wait is exhausted it raises and gives up — there is no retry above
it. In a single process running several concurrent writers (request handling
plus background loops), that collision is routine, not exceptional, so callers
route their write transactions through `run_in_transaction` instead of opening
`Database.transaction()` directly.

>>> async def _append(tx: Transaction) -> Row:
...     return await tx.execute(insert(Row(...)).returning())
>>> row = await run_in_transaction(database, _append)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import random
from sqlite3 import OperationalError

import structlog
from snekql.sqlite import Database, DatabaseRuntimeError, Transaction

_logger = structlog.stdlib.get_logger("tether.db_retry")

_MAX_ATTEMPTS = 4
"""Total transaction attempts before a locked database is treated as a real failure."""

_INITIAL_BACKOFF_SECONDS = 0.02
"""Delay before the first retry; doubles each subsequent attempt, plus jitter."""

_LOCKED_DATABASE_MESSAGE = "database is locked"
"""The `sqlite3.OperationalError` text snekql's busy_timeout exhaustion raises."""


def _is_lock_contention(error: DatabaseRuntimeError) -> bool:
    """Report whether `error` is SQLite write-lock contention, not a real failure.

    Only `sqlite3.OperationalError("database is locked")` chained as the cause
    qualifies; constraint violations, schema errors, and closed-transaction
    misuse are also `DatabaseRuntimeError` subclasses but must not be retried.
    """
    cause = error.__cause__
    return isinstance(cause, OperationalError) and _LOCKED_DATABASE_MESSAGE in str(
        cause
    )


async def run_in_transaction[T](
    database: Database,
    transaction_body: Callable[[Transaction], Awaitable[T]],
) -> T:
    """Run `transaction_body` in a fresh transaction, retrying on write-lock contention.

    Each retry reopens the transaction and reruns `transaction_body` from
    scratch, so it must be safe to repeat (the usual transaction contract: no
    observable effects beyond its own writes). Failures other than write-lock
    contention propagate on the first attempt.
    """
    attempt = 0
    while True:
        try:
            async with database.transaction() as tx:
                return await transaction_body(tx)
        except DatabaseRuntimeError as error:
            attempt += 1
            if not _is_lock_contention(error):
                raise
            if attempt >= _MAX_ATTEMPTS:
                _logger.warning(
                    "db_write_lock_contention_exhausted",
                    attempts=attempt,
                    error=str(error),
                )
                raise
            backoff_seconds = _INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            delay_seconds = backoff_seconds * (1 + random())
            _logger.debug(
                "db_write_lock_contention_retry",
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
            await asyncio.sleep(delay_seconds)
