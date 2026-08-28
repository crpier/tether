"""Structured ownership for process-local application background work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Self

from anyio import create_task_group, move_on_after, sleep
from anyio.abc import TaskGroup

from tether.structured_logging import Logger


class BackgroundBootOutcome(StrEnum):
    """Whether successful deferred boot should enter periodic execution."""

    REPEAT = "repeat"
    STOP = "stop"


class BackgroundFailurePolicy(StrEnum):
    """How the host responds when registered work fails unexpectedly."""

    FAIL_HOST = "fail_host"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class BackgroundSchedule:
    """Cadence and failure behavior for one repeatable pass."""

    failure_policy: BackgroundFailurePolicy
    initial_delay_seconds: float
    interval_seconds: float


class BackgroundTaskState(StrEnum):
    """Current process-local state of one registered background task."""

    DISABLED = "disabled"
    FAILED = "failed"
    REGISTERED = "registered"
    RETRYING = "retrying"
    RUNNING = "running"
    STOPPED = "stopped"
    WAITING = "waiting"


@dataclass(frozen=True, slots=True)
class BackgroundTaskHealth:
    """Typed diagnostic state for one named task."""

    last_failed_at: datetime | None
    last_failure: str | None
    last_started_at: datetime | None
    last_succeeded_at: datetime | None
    name: str
    state: BackgroundTaskState


@dataclass(frozen=True, slots=True)
class BackgroundHealthSnapshot:
    """Immutable health state for every registered background task."""

    tasks: tuple[BackgroundTaskHealth, ...]


class BackgroundRegistrationError(Exception):
    """A background task registration violates the runtime interface."""

    def __init__(self, name: str, *, runtime_started: bool = False) -> None:
        message = (
            f"background task cannot be registered after runtime start: {name}"
            if runtime_started
            else f"background task name is already registered: {name}"
        )
        super().__init__(message)


class _BackgroundWorkerExitedError(Exception):
    """A long-lived background worker returned while the host was running."""

    def __init__(self, name: str) -> None:
        super().__init__(f"background worker returned unexpectedly: {name}")


@dataclass(frozen=True, slots=True)
class _PeriodicRegistration:
    """One fixed-cadence pass hidden behind the runtime interface."""

    boot: Callable[[], Awaitable[BackgroundBootOutcome]] | None
    name: str
    schedule: BackgroundSchedule
    operation: Callable[[], Awaitable[object]]
    readiness: asyncio.Event


@dataclass(frozen=True, slots=True)
class _WorkerRegistration:
    """One long-lived domain worker hidden behind the runtime interface."""

    failure_policy: BackgroundFailurePolicy
    name: str
    operation: Callable[[], Awaitable[object]]
    readiness: asyncio.Event


class BackgroundRuntime:
    """Own named application-lifetime work and its diagnostic state.

    Example:
        runtime = BackgroundRuntime(logger)
        readiness = runtime.register_disabled("optional-source")
        assert readiness.is_set()
    """

    def __init__(
        self,
        logger: Logger,
        *,
        retry_base_seconds: float = 1.0,
        retry_cap_seconds: float = 60.0,
        shutdown_grace_seconds: float = 0.5,
    ) -> None:
        self._active_periodic: set[str] = set()
        self._health: dict[str, BackgroundTaskHealth] = {}
        self._logger: Logger = logger
        self._periodic: list[_PeriodicRegistration] = []
        self._periodic_idle: asyncio.Event = asyncio.Event()
        self._periodic_idle.set()
        self._readiness: dict[str, asyncio.Event] = {}
        self._retry_base_seconds: float = retry_base_seconds
        self._retry_cap_seconds: float = retry_cap_seconds
        self._shutdown_grace_seconds: float = shutdown_grace_seconds
        self._stopping: bool = False
        self._task_group: TaskGroup | None = None
        self._workers: list[_WorkerRegistration] = []

    async def __aenter__(self) -> Self:
        """Start every registered task inside one AnyIO task group."""
        task_group = create_task_group()
        _ = await task_group.__aenter__()
        self._task_group = task_group
        for registration in self._periodic:
            _ = task_group.start_soon(
                self._run_periodic,
                registration,
                name=registration.name,
            )
        for registration in self._workers:
            _ = task_group.start_soon(
                self._run_worker,
                registration,
                name=registration.name,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Cancel owned work before the surrounding resource graph closes."""
        task_group = self._task_group
        if task_group is None:
            return None
        self._stopping = True
        if self._active_periodic:
            with move_on_after(self._shutdown_grace_seconds, shield=True) as drain:
                _ = await self._periodic_idle.wait()
            if drain.cancelled_caught:
                self._logger.warning(
                    "Background periodic work exceeded shutdown grace; cancelling",
                    tasks=sorted(self._active_periodic),
                )
        task_group.cancel_scope.cancel()
        self._task_group = None
        try:
            return await task_group.__aexit__(exc_type, exc_value, traceback)
        finally:
            for name, health in self._health.items():
                if health.state not in {
                    BackgroundTaskState.DISABLED,
                    BackgroundTaskState.FAILED,
                    BackgroundTaskState.STOPPED,
                }:
                    self._health[name] = replace(
                        health, state=BackgroundTaskState.STOPPED
                    )

    def register_disabled(self, name: str) -> asyncio.Event:
        """Register disabled optional work and release readiness immediately."""
        readiness = self._register_name(name)
        self._health[name] = replace(
            self._health[name], state=BackgroundTaskState.DISABLED
        )
        readiness.set()
        return readiness

    def register_worker(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
        *,
        failure_policy: BackgroundFailurePolicy,
    ) -> asyncio.Event:
        """Register one long-lived domain worker under runtime ownership."""
        readiness = self._register_name(name)
        self._workers.append(
            _WorkerRegistration(
                failure_policy=failure_policy,
                name=name,
                operation=operation,
                readiness=readiness,
            )
        )
        return readiness

    def register_periodic(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
        *,
        schedule: BackgroundSchedule,
        boot: Callable[[], Awaitable[BackgroundBootOutcome]] | None = None,
    ) -> asyncio.Event:
        """Register one pass for runtime-owned fixed-cadence execution."""
        readiness = self._register_name(name)
        self._periodic.append(
            _PeriodicRegistration(
                boot=boot,
                name=name,
                operation=operation,
                readiness=readiness,
                schedule=schedule,
            )
        )
        return readiness

    def readiness(self, name: str) -> asyncio.Event:
        """Return one registered task's startup barrier."""
        return self._readiness[name]

    def snapshot(self) -> BackgroundHealthSnapshot:
        """Return immutable task health ordered by diagnostic name."""
        return BackgroundHealthSnapshot(
            tasks=tuple(self._health[name] for name in sorted(self._health))
        )

    def _register_name(self, name: str) -> asyncio.Event:
        """Reserve one stable diagnostic identity before work can start."""
        if self._task_group is not None:
            raise BackgroundRegistrationError(name, runtime_started=True)
        if name in self._readiness:
            raise BackgroundRegistrationError(name)
        readiness = asyncio.Event()
        self._health[name] = BackgroundTaskHealth(
            last_failed_at=None,
            last_failure=None,
            last_started_at=None,
            last_succeeded_at=None,
            name=name,
            state=BackgroundTaskState.REGISTERED,
        )
        self._readiness[name] = readiness
        return readiness

    async def _run_worker(self, registration: _WorkerRegistration) -> None:
        """Restart an isolated worker when it fails or returns unexpectedly."""
        registration.readiness.set()
        consecutive_failures = 0
        while True:
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_started_at=datetime.now(UTC),
                state=BackgroundTaskState.RUNNING,
            )
            try:
                _ = await registration.operation()
            except Exception as error:
                worker_error = error
            else:
                worker_error = _BackgroundWorkerExitedError(registration.name)
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_failed_at=datetime.now(UTC),
                last_failure=f"{type(worker_error).__name__}: {worker_error}",
                state=BackgroundTaskState.FAILED,
            )
            if registration.failure_policy is BackgroundFailurePolicy.FAIL_HOST:
                self._logger.error(
                    "Critical background task failed",
                    task=registration.name,
                    error_type=type(worker_error).__name__,
                )
                raise worker_error
            consecutive_failures += 1
            retry_seconds = min(
                self._retry_base_seconds * (2 ** (consecutive_failures - 1)),
                self._retry_cap_seconds,
            )
            self._health[registration.name] = replace(
                self._health[registration.name],
                state=BackgroundTaskState.RETRYING,
            )
            self._logger.error(
                "Background worker stopped; retrying",
                task=registration.name,
                error_type=type(worker_error).__name__,
                retry_seconds=retry_seconds,
            )
            await sleep(retry_seconds)

    async def _run_periodic(
        self,
        registration: _PeriodicRegistration,
    ) -> None:
        """Run fresh finite passes and expose a brief shutdown drain window."""
        if registration.boot is not None:
            if not await self._run_periodic_boot(registration):
                return
        else:
            registration.readiness.set()
            self._health[registration.name] = replace(
                self._health[registration.name], state=BackgroundTaskState.WAITING
            )
        if self._stopping:
            return
        await sleep(registration.schedule.initial_delay_seconds)
        consecutive_failures = 0
        while not self._stopping:
            consecutive_failures, retry_seconds = await self._run_periodic_pass(
                registration,
                consecutive_failures=consecutive_failures,
            )
            if self._stopping:
                return
            await sleep(
                retry_seconds
                if retry_seconds is not None
                else registration.schedule.interval_seconds
            )

    async def _run_periodic_boot(
        self,
        registration: _PeriodicRegistration,
    ) -> bool:
        """Run deferred boot without allowing it to block host startup."""
        assert registration.boot is not None
        self._begin_periodic_pass(registration.name)
        try:
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_started_at=datetime.now(UTC),
                state=BackgroundTaskState.RUNNING,
            )
            try:
                boot_outcome = await registration.boot()
            except Exception as error:
                self._health[registration.name] = replace(
                    self._health[registration.name],
                    last_failed_at=datetime.now(UTC),
                    last_failure=f"{type(error).__name__}: {error}",
                    state=BackgroundTaskState.FAILED,
                )
                registration.readiness.set()
                if (
                    registration.schedule.failure_policy
                    is BackgroundFailurePolicy.FAIL_HOST
                ):
                    self._logger.exception(
                        "Critical background task failed",
                        task=registration.name,
                        error_type=type(error).__name__,
                    )
                    raise
                self._health[registration.name] = replace(
                    self._health[registration.name],
                    state=BackgroundTaskState.RETRYING,
                )
                self._logger.exception(
                    "Background task boot failed; periodic retries remain enabled",
                    task=registration.name,
                    error_type=type(error).__name__,
                )
                return True
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_succeeded_at=datetime.now(UTC),
                state=BackgroundTaskState.WAITING,
            )
            registration.readiness.set()
            if boot_outcome is BackgroundBootOutcome.STOP:
                self._health[registration.name] = replace(
                    self._health[registration.name],
                    state=BackgroundTaskState.STOPPED,
                )
                return False
            return True
        finally:
            self._end_periodic_pass(registration.name)

    async def _run_periodic_pass(
        self,
        registration: _PeriodicRegistration,
        *,
        consecutive_failures: int,
    ) -> tuple[int, float | None]:
        """Run and record one finite pass, returning its next retry delay."""
        self._begin_periodic_pass(registration.name)
        try:
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_started_at=datetime.now(UTC),
                state=BackgroundTaskState.RUNNING,
            )
            try:
                _ = await registration.operation()
            except Exception as error:
                self._health[registration.name] = replace(
                    self._health[registration.name],
                    last_failed_at=datetime.now(UTC),
                    last_failure=f"{type(error).__name__}: {error}",
                    state=BackgroundTaskState.FAILED,
                )
                if (
                    registration.schedule.failure_policy
                    is BackgroundFailurePolicy.FAIL_HOST
                ):
                    self._logger.exception(
                        "Critical background task failed",
                        task=registration.name,
                        error_type=type(error).__name__,
                    )
                    raise
                next_failure_count = consecutive_failures + 1
                retry_seconds = min(
                    self._retry_base_seconds * (2 ** (next_failure_count - 1)),
                    self._retry_cap_seconds,
                )
                self._health[registration.name] = replace(
                    self._health[registration.name],
                    state=BackgroundTaskState.RETRYING,
                )
                self._logger.exception(
                    "Background task failed; retrying",
                    task=registration.name,
                    error_type=type(error).__name__,
                    retry_seconds=retry_seconds,
                )
                return next_failure_count, retry_seconds
            self._health[registration.name] = replace(
                self._health[registration.name],
                last_succeeded_at=datetime.now(UTC),
                state=BackgroundTaskState.WAITING,
            )
            return 0, None
        finally:
            self._end_periodic_pass(registration.name)

    def _begin_periodic_pass(self, name: str) -> None:
        """Mark finite work active so shutdown can briefly drain it."""
        self._active_periodic.add(name)
        self._periodic_idle.clear()

    def _end_periodic_pass(self, name: str) -> None:
        """Release the shared drain barrier when all finite work settles."""
        self._active_periodic.discard(name)
        if not self._active_periodic:
            self._periodic_idle.set()
