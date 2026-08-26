"""Behavior tests for the shared Ingestion gate lifecycle."""

import asyncio

import structlog
from snektest import assert_eq, assert_false, assert_true, test

from tether.ingestion_lifecycle import (
    IngestionBootOutcome,
    IngestionLifecycle,
    IngestionWorker,
)


class StoppedAfterBootWorker(IngestionWorker):
    """An adapter whose source preflight disables repetition for this process."""

    def __init__(self) -> None:
        self.repeat_called = False

    async def boot(self) -> IngestionBootOutcome:
        return IngestionBootOutcome.STOP

    async def repeat(self) -> None:
        self.repeat_called = True


class BootFailure(Exception):
    """A source boot failure used by the lifecycle fake."""


class FailedBootWorker(IngestionWorker):
    """An adapter whose idempotent boot pass fails transiently."""

    def __init__(self) -> None:
        self.repeat_started = asyncio.Event()

    async def boot(self) -> IngestionBootOutcome:
        raise BootFailure("upstream unavailable")

    async def repeat(self) -> None:
        self.repeat_started.set()
        await asyncio.Event().wait()


class CancellableWorker(IngestionWorker):
    """An adapter that exposes periodic-loop cancellation."""

    def __init__(self) -> None:
        self.repeat_started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def boot(self) -> IngestionBootOutcome:
        return IngestionBootOutcome.REPEAT

    async def repeat(self) -> None:
        self.repeat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


class StubbornWorker(IngestionWorker):
    """An adapter that cannot finish until an upstream call returns."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.repeat_started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def boot(self) -> IngestionBootOutcome:
        return IngestionBootOutcome.REPEAT

    async def repeat(self) -> None:
        self.repeat_started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            self.stopped.set()


class BlockingWorker(IngestionWorker):
    """An Ingestion adapter whose boot waits for an upstream response."""

    def __init__(self) -> None:
        self.boot_started = asyncio.Event()
        self.release_boot = asyncio.Event()
        self.repeat_started = asyncio.Event()

    async def boot(self) -> IngestionBootOutcome:
        self.boot_started.set()
        await self.release_boot.wait()
        return IngestionBootOutcome.REPEAT

    async def repeat(self) -> None:
        self.repeat_started.set()
        await asyncio.Event().wait()


@test()
async def activation_returns_before_boot_completes() -> None:
    """A slow upstream boot never blocks host startup."""
    worker = BlockingWorker()
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )

    readiness = lifecycle.activate("slow-source", worker)
    await asyncio.wait_for(worker.boot_started.wait(), timeout=0.1)

    assert_false(readiness.is_set())
    worker.release_boot.set()
    await asyncio.wait_for(readiness.wait(), timeout=0.1)
    assert_true(worker.repeat_started.is_set())
    await lifecycle.stop()


@test()
async def boot_can_disable_repetition_for_the_process() -> None:
    """A rejected source preflight releases readiness without polling upstream."""
    worker = StoppedAfterBootWorker()
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )

    readiness = lifecycle.activate("rejected-source", worker)
    await asyncio.wait_for(readiness.wait(), timeout=0.1)
    await asyncio.sleep(0)

    assert_false(worker.repeat_called)
    await lifecycle.stop()


@test()
async def boot_failure_releases_readiness_and_starts_periodic_retries() -> None:
    """A transient source failure cannot block startup or disable later retries."""
    worker = FailedBootWorker()
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )

    readiness = lifecycle.activate("failing-source", worker)
    await asyncio.wait_for(readiness.wait(), timeout=0.1)
    await asyncio.wait_for(worker.repeat_started.wait(), timeout=0.1)

    assert_true(readiness.is_set())
    await lifecycle.stop()


@test()
async def stop_cancels_periodic_execution() -> None:
    """The lifecycle owner stops every active source worker on shutdown."""
    worker = CancellableWorker()
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )
    _ = lifecycle.activate("periodic-source", worker)
    await asyncio.wait_for(worker.repeat_started.wait(), timeout=0.1)

    await lifecycle.stop()

    assert_true(worker.stopped.is_set())


@test()
async def stop_abandons_a_worker_after_the_grace_period() -> None:
    """One blocked upstream cannot hold host shutdown open indefinitely."""
    worker = StubbornWorker()
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )
    _ = lifecycle.activate("stubborn-source", worker)
    await asyncio.wait_for(worker.repeat_started.wait(), timeout=0.1)

    await lifecycle.stop(grace_seconds=0.01)

    assert_false(worker.stopped.is_set())
    worker.release.set()
    await asyncio.wait_for(worker.stopped.wait(), timeout=0.1)
    await lifecycle.stop(grace_seconds=0.1)


@test()
async def inactive_gate_is_immediately_ready_without_a_task() -> None:
    """A disabled or unconfigured source has no worker and is ready immediately."""
    lifecycle = IngestionLifecycle(
        logger=structlog.stdlib.get_logger("test.ingestion_lifecycle")
    )

    readiness = lifecycle.activate("disabled-source")

    assert_true(readiness.is_set())
    assert_eq(lifecycle.readiness("disabled-source"), readiness)
    await lifecycle.stop()
