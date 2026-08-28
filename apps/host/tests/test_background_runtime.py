"""Behavior tests for process-local application background work."""

import asyncio
import contextlib
import time

import structlog
from snektest import (
    assert_eq,
    assert_is_not_none,
    assert_raises,
    assert_true,
    test,
)
from structlog.testing import capture_logs

from tether.background_runtime import (
    BackgroundBootOutcome,
    BackgroundFailurePolicy,
    BackgroundRegistrationError,
    BackgroundRuntime,
    BackgroundSchedule,
    BackgroundTaskState,
)


@test(mark="fast")
def disabled_work_is_ready_without_starting() -> None:
    """A disabled optional worker cannot hold its readiness barrier."""
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    readiness = runtime.register_disabled("disabled-source")

    assert_true(readiness.is_set())
    assert_true(runtime.readiness("disabled-source").is_set())


@test(mark="fast")
def task_names_are_unique() -> None:
    """Duplicate diagnostic names are rejected before background work starts."""
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )
    _ = runtime.register_disabled("same-name")

    with assert_raises(BackgroundRegistrationError):
        _ = runtime.register_disabled("same-name")


class RetryableWorkFailure(Exception):
    """A transient failure from safely repeatable test work."""


@test(mark="fast")
async def readiness_waits_for_deferred_boot() -> None:
    """Slow optional-source boot stays outside startup but gates readiness."""
    boot_started = asyncio.Event()
    release_boot = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def boot() -> BackgroundBootOutcome:
        boot_started.set()
        await release_boot.wait()
        return BackgroundBootOutcome.REPEAT

    async def run_pass() -> None:
        return None

    readiness = runtime.register_periodic(
        "deferred-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=60.0,
            interval_seconds=60.0,
        ),
        boot=boot,
    )

    async with runtime:
        await asyncio.wait_for(boot_started.wait(), timeout=0.1)
        assert_true(not readiness.is_set())
        release_boot.set()
        await asyncio.wait_for(readiness.wait(), timeout=0.1)

    assert_true(readiness.is_set())


@test(mark="fast")
async def retry_backoff_escalates_only_to_its_cap() -> None:
    """Repeatable work retries with an increasing bounded delay."""
    attempts = 0
    completed = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=0.001,
        retry_cap_seconds=0.002,
    )

    async def run_pass() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise RetryableWorkFailure("still unavailable")
        completed.set()

    _ = runtime.register_periodic(
        "backoff-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    with capture_logs() as logs:
        async with runtime:
            await asyncio.wait_for(completed.wait(), timeout=0.1)

    assert_eq(
        [log["retry_seconds"] for log in logs if "retry_seconds" in log],
        [0.001, 0.002, 0.002],
    )


@test(mark="fast")
async def retryable_boot_failure_releases_readiness() -> None:
    """Failed optional-source boot cannot block later periodic attempts."""
    periodic_started = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=0.001,
        retry_cap_seconds=0.001,
    )

    async def boot() -> BackgroundBootOutcome:
        raise RetryableWorkFailure("boot unavailable")

    async def run_pass() -> None:
        periodic_started.set()

    readiness = runtime.register_periodic(
        "failed-boot",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.001,
            interval_seconds=60.0,
        ),
        boot=boot,
    )

    async with runtime:
        await asyncio.wait_for(readiness.wait(), timeout=0.1)
        await asyncio.wait_for(periodic_started.wait(), timeout=0.1)

    assert_true(readiness.is_set())


@test(mark="fast")
async def periodic_work_runs_under_runtime_ownership() -> None:
    """Entering the runtime starts registered periodic work."""
    ran = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def run_pass() -> None:
        ran.set()

    _ = runtime.register_periodic(
        "periodic-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    async with runtime:
        await asyncio.wait_for(ran.wait(), timeout=0.1)

    assert_true(ran.is_set())


@test(mark="fast")
async def worker_return_is_treated_as_a_failure() -> None:
    """A long-lived worker cannot disappear by returning without notice."""
    starts = 0
    restarted = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=0.001,
        retry_cap_seconds=0.001,
    )

    async def run_worker() -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            restarted.set()

    _ = runtime.register_worker(
        "returning-worker",
        run_worker,
        failure_policy=BackgroundFailurePolicy.RETRY,
    )

    async with runtime:
        await asyncio.wait_for(restarted.wait(), timeout=0.1)

    assert_eq(starts, 2)


@test(mark="fast")
async def shutdown_repeats_cancellation_for_resistant_work() -> None:
    """One swallowed cancellation cannot hold structured shutdown open."""
    started = asyncio.Event()
    stopped = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def resist_once() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    _ = runtime.register_worker(
        "resistant-worker",
        resist_once,
        failure_policy=BackgroundFailurePolicy.FAIL_HOST,
    )

    before = time.monotonic()
    async with runtime:
        await asyncio.wait_for(started.wait(), timeout=0.1)
    elapsed = time.monotonic() - before

    assert_true(stopped.is_set())
    assert_true(elapsed < 0.2)


@test(mark="fast")
async def shutdown_briefly_drains_an_active_periodic_pass() -> None:
    """Shutdown lets bounded finite work finish before escalating cancellation."""
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    cancelled = False
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        shutdown_grace_seconds=0.1,
    )

    async def run_pass() -> None:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        completed.set()

    _ = runtime.register_periodic(
        "drained-pass",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    async with runtime:
        await asyncio.wait_for(started.wait(), timeout=0.1)
        _ = asyncio.get_running_loop().call_later(0.01, release.set)

    assert_true(completed.is_set())
    assert_true(not cancelled)


@test(mark="fast")
async def runtime_stops_work_before_later_resources_close() -> None:
    """Resource unwinding stops background work before closing its dependency."""
    events: list[str] = []
    started = asyncio.Event()
    resources = contextlib.AsyncExitStack()
    await resources.__aenter__()

    async def close_database() -> None:
        events.append("database-closed")

    _ = resources.push_async_callback(close_database)
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("worker-stopped")

    _ = runtime.register_worker(
        "ordered-worker",
        worker,
        failure_policy=BackgroundFailurePolicy.FAIL_HOST,
    )
    _ = await resources.enter_async_context(runtime)
    await asyncio.wait_for(started.wait(), timeout=0.1)

    await resources.aclose()

    assert_eq(events, ["worker-stopped", "database-closed"])


@test(mark="fast")
async def health_snapshot_reports_stopped_after_shutdown() -> None:
    """A completed runtime no longer reports cancelled work as live."""
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def run_pass() -> None:
        return None

    _ = runtime.register_periodic(
        "stopped-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=60.0,
            interval_seconds=60.0,
        ),
    )

    async with runtime:
        await asyncio.sleep(0)

    assert_eq(runtime.snapshot().tasks[0].state, BackgroundTaskState.STOPPED)


@test(mark="fast")
async def health_snapshot_reports_worker_failure() -> None:
    """Worker diagnostics retain the latest start and failure details."""
    failed = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=60.0,
    )

    async def worker() -> None:
        failed.set()
        raise RetryableWorkFailure("worker defect")

    _ = runtime.register_worker(
        "unhealthy-worker",
        worker,
        failure_policy=BackgroundFailurePolicy.RETRY,
    )

    async with runtime:
        await asyncio.wait_for(failed.wait(), timeout=0.1)
        await asyncio.sleep(0)
        task_health = runtime.snapshot().tasks[0]

        assert_eq(task_health.state, BackgroundTaskState.RETRYING)
        assert_is_not_none(task_health.last_started_at)
        assert_is_not_none(task_health.last_failed_at)
        assert_eq(task_health.last_failure, "RetryableWorkFailure: worker defect")


@test(mark="fast")
async def health_snapshot_reports_a_successful_pass() -> None:
    """Callers can inspect the last successful execution of named work."""
    passed = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def run_pass() -> None:
        passed.set()

    _ = runtime.register_periodic(
        "healthy-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    async with runtime:
        await asyncio.wait_for(passed.wait(), timeout=0.1)
        await asyncio.sleep(0)
        task_health = runtime.snapshot().tasks[0]

        assert_eq(task_health.name, "healthy-work")
        assert_eq(task_health.state, BackgroundTaskState.WAITING)
        assert_is_not_none(task_health.last_started_at)
        assert_is_not_none(task_health.last_succeeded_at)


@test(mark="fast")
async def critical_failure_exits_the_runtime() -> None:
    """Critical task failure cancels sibling work and reaches the host owner."""
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def fail() -> None:
        raise RetryableWorkFailure("critical defect")

    _ = runtime.register_periodic(
        "critical-work",
        fail,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.FAIL_HOST,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    with assert_raises(BaseExceptionGroup) as raised:
        async with runtime:
            await asyncio.Event().wait()

    assert_is_not_none(raised.exception.subgroup(RetryableWorkFailure))


@test(mark="fast")
async def critical_failure_is_logged_before_host_exit() -> None:
    """A critical defect is visible at the moment the runtime stops the host."""
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime")
    )

    async def fail() -> None:
        raise RetryableWorkFailure("critical defect")

    _ = runtime.register_periodic(
        "logged-critical-work",
        fail,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.FAIL_HOST,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    with capture_logs() as logs, assert_raises(BaseExceptionGroup):
        async with runtime:
            await asyncio.Event().wait()

    assert_true(
        any(
            log["event"] == "Critical background task failed"
            and log["task"] == "logged-critical-work"
            for log in logs
        )
    )


@test(mark="fast")
async def retryable_failure_does_not_stop_unrelated_work() -> None:
    """One isolated defect cannot cancel another registered task."""
    healthy_ran = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=60.0,
    )

    async def fail() -> None:
        raise RetryableWorkFailure("isolated defect")

    async def healthy() -> None:
        healthy_ran.set()

    retry_schedule = BackgroundSchedule(
        failure_policy=BackgroundFailurePolicy.RETRY,
        initial_delay_seconds=0.0,
        interval_seconds=60.0,
    )
    _ = runtime.register_periodic("isolated-failure", fail, schedule=retry_schedule)
    _ = runtime.register_periodic("healthy-work", healthy, schedule=retry_schedule)

    async with runtime:
        await asyncio.wait_for(healthy_ran.wait(), timeout=0.1)

    assert_true(healthy_ran.is_set())


@test(mark="fast")
async def retry_policy_repeats_a_failed_pass() -> None:
    """A safe failed pass retries without terminating its runtime."""
    attempts = 0
    retried = asyncio.Event()
    runtime = BackgroundRuntime(
        logger=structlog.stdlib.get_logger("test.background_runtime"),
        retry_base_seconds=0.001,
        retry_cap_seconds=0.001,
    )

    async def run_pass() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableWorkFailure("temporary failure")
        retried.set()

    _ = runtime.register_periodic(
        "retrying-work",
        run_pass,
        schedule=BackgroundSchedule(
            failure_policy=BackgroundFailurePolicy.RETRY,
            initial_delay_seconds=0.0,
            interval_seconds=60.0,
        ),
    )

    async with runtime:
        await asyncio.wait_for(retried.wait(), timeout=0.1)

    assert_eq(attempts, 2)
