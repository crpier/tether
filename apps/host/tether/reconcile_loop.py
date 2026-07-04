"""The shared periodic loop both reconcilers run their full passes on.

`SearchReconciler` and `TranscriptReconciler` have domain-specific `reconcile`
bodies (stored vectors + model marker vs. re-derive-from-canonical chunks), but
the forever loop around them is identical: sleep, run a pass, log and swallow a
failure so the next tick retries. This module owns that loop once. Other
forever loops in the host (`youtube.py` liked-videos sync, `chat_engine.py`
idle-session reaper) share the shape but carry their own pacing/backoff
concerns and keep their own loops.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tether.logging import Logger


async def run_reconcile_loop(
    reconcile: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float,
    initial_delay_seconds: float,
    logger: Logger,
    failure_message: str,
) -> None:
    """Run `reconcile` forever: after `initial_delay_seconds`, once per interval.

    `reconcile` is a zero-arg factory returning a fresh awaitable each pass (a
    coroutine object could only be awaited once); its result is discarded. A
    failed pass is logged under `failure_message` and swallowed so a transient
    error never kills the loop — the next tick retries. Only `Exception` is
    caught: `asyncio.CancelledError` is a `BaseException`, so cancellation
    surfaces out of the sleep or a running pass and stops the loop cleanly."""
    delay = initial_delay_seconds
    while True:
        await asyncio.sleep(delay)
        try:
            _ = await reconcile()
        except Exception:
            logger.exception(failure_message)
        delay = interval_seconds
