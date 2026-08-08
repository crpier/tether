"""Shared activation lifecycle for optional Ingestion gates."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tether.logging import Logger


class IngestionBootOutcome(StrEnum):
    """Whether a successfully completed boot should enter periodic execution."""

    REPEAT = "repeat"
    STOP = "stop"


class IngestionWorker(Protocol):
    """Source adapter consumed by the shared Ingestion lifecycle.

    Example:
        class Worker:
            async def boot(self) -> IngestionBootOutcome:
                return IngestionBootOutcome.REPEAT

            async def repeat(self) -> None:
                await asyncio.Event().wait()
    """

    async def boot(self) -> IngestionBootOutcome:
        """Run one idempotent pass outside the host startup critical path."""
        ...

    async def repeat(self) -> None:
        """Run source-specific periodic ingestion until cancelled."""
        ...


@dataclass(frozen=True, slots=True)
class CallbackIngestionWorker:
    """Adapt source-specific boot and repeat callables to `IngestionWorker`."""

    boot_callback: Callable[[], Awaitable[IngestionBootOutcome]]
    repeat_callback: Callable[[], Awaitable[None]]

    async def boot(self) -> IngestionBootOutcome:
        """Run the configured source boot pass."""
        return await self.boot_callback()

    async def repeat(self) -> None:
        """Run the configured source periodic loop."""
        await self.repeat_callback()


class IngestionLifecycle:
    """Own deferred boot, readiness, repetition, and cancellation for gates.

    Example:
        lifecycle = IngestionLifecycle(logger)
        readiness = lifecycle.activate("source", worker)
        await readiness.wait()
        await lifecycle.stop()
    """

    def __init__(self, logger: Logger) -> None:
        self._logger: Logger = logger
        self._readiness: dict[str, asyncio.Event] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def activate(
        self, name: str, worker: IngestionWorker | None = None
    ) -> asyncio.Event:
        """Register one gate and defer its boot when an adapter is active."""
        readiness = asyncio.Event()
        self._readiness[name] = readiness
        if worker is None:
            readiness.set()
            return readiness
        self._tasks.append(
            asyncio.create_task(
                self._run(name=name, readiness=readiness, worker=worker),
                name=f"ingestion:{name}",
            )
        )
        return readiness

    def readiness(self, name: str) -> asyncio.Event:
        """Return the boot barrier for one registered gate."""
        return self._readiness[name]

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        """Cancel active gates without letting one upstream block shutdown."""
        for task in self._tasks:
            _ = task.cancel()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=grace_seconds)
        for task in pending:
            self._logger.warning(
                "Ingestion gate did not stop within the shutdown grace period",
                task=task.get_name(),
            )
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(
        self, *, name: str, readiness: asyncio.Event, worker: IngestionWorker
    ) -> None:
        """Release readiness after boot even when one upstream fails."""
        try:
            outcome = await worker.boot()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Ingestion gate boot failed", gate=name)
            outcome = IngestionBootOutcome.REPEAT
        finally:
            readiness.set()
        if outcome is IngestionBootOutcome.REPEAT:
            await worker.repeat()
