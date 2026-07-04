"""Behavior tests for the shared reconcile loop helper.

`run_reconcile_loop` is the one genuinely shared piece of the two reconcilers:
the swallow-and-retry periodic loop. It is driven here directly with fake
reconcile callables — no database, no index — proving the loop contract once:
an initial delay, a pass per tick, a failed pass logged and swallowed, and
cancellation escaping cleanly (even mid-pass).
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from snektest import assert_true, test
from structlog.testing import capture_logs

from tether.logging import Logger
from tether.reconcile_loop import run_reconcile_loop


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.reconcile_loop")


class _TransientPassError(Exception):
    """Stands in for any failure a single reconcile pass may raise."""


@test()
async def run_reconcile_loop_runs_passes_until_cancelled() -> None:
    """The loop awaits a fresh pass from the factory on every tick."""
    passes = 0

    async def reconcile() -> None:
        nonlocal passes
        passes += 1

    task = asyncio.create_task(
        run_reconcile_loop(
            reconcile,
            interval_seconds=0.001,
            initial_delay_seconds=0.001,
            logger=_logger(),
            failure_message="unused",
        )
    )
    for _ in range(1000):  # bounded wait so a broken loop fails fast, never hangs
        if passes >= 3:
            break
        await asyncio.sleep(0.001)
    _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert_true(passes >= 3)


@test()
async def run_reconcile_loop_retries_after_a_failed_pass() -> None:
    """A failed pass is logged under `failure_message` and the next tick retries."""
    passes = 0

    async def reconcile() -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise _TransientPassError("first pass blows up")

    with capture_logs() as logs:
        task = asyncio.create_task(
            run_reconcile_loop(
                reconcile,
                interval_seconds=0.001,
                initial_delay_seconds=0.001,
                logger=_logger(),
                failure_message="Test reconcile failed; retrying next tick",
            )
        )
        for _ in range(1000):  # bounded wait so a broken loop fails fast
            if passes >= 2:
                break
            await asyncio.sleep(0.001)
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert_true(passes >= 2)
    assert_true(
        any(log["event"] == "Test reconcile failed; retrying next tick" for log in logs)
    )


@test()
async def run_reconcile_loop_honors_initial_delay() -> None:
    """The first pass waits `initial_delay_seconds`, not a full interval."""
    first_pass_ran = asyncio.Event()

    async def reconcile() -> None:
        first_pass_ran.set()

    task = asyncio.create_task(
        run_reconcile_loop(
            reconcile,
            # A full-interval first wait would blow the bounded wait below.
            interval_seconds=60.0,
            initial_delay_seconds=0.001,
            logger=_logger(),
            failure_message="unused",
        )
    )
    await asyncio.wait_for(first_pass_ran.wait(), timeout=1.0)
    _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert_true(first_pass_ran.is_set())


@test()
async def run_reconcile_loop_stops_on_cancellation_mid_pass() -> None:
    """Cancellation escapes the swallow guard even while a pass is in flight."""
    pass_started = asyncio.Event()

    async def reconcile() -> None:
        pass_started.set()
        await asyncio.Event().wait()  # never set: a pass that hangs forever

    task = asyncio.create_task(
        run_reconcile_loop(
            reconcile,
            interval_seconds=0.001,
            initial_delay_seconds=0.001,
            logger=_logger(),
            failure_message="unused",
        )
    )
    await asyncio.wait_for(pass_started.wait(), timeout=1.0)
    _ = task.cancel()
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True

    assert_true(cancelled)
