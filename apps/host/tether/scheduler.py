"""In-process Scheduled-trigger scheduler: poll, claim, dispatch, back off.

Durability lives in the loop plus SQLite, with no external broker. Every tick
asks the `TriggerService` for due rows, which are stamped `claimed` before any
dispatch so a row in flight is never picked up twice. Each claimed trigger is
dispatched as an `asyncio` task gated behind a concurrency semaphore
(backpressure); a successful dispatch settles the row (a `once` trigger
completes, a recurring one re-arms) and a failed one is backed off via
`next_attempt_at` for a later retry.

Dispatch itself is a `TriggerDispatcher`: a fixed-message trigger stores its
payload as a durable notification, while an agent-prompt trigger runs as a
normal turn in the default Conversation. The resulting assistant message
also retains configured Web Push delivery.

The loop takes its time from a `Clock`, so tests drive it with a controlled
clock and a fake dispatcher and assert fire + retry behaviour without sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from snekql.sqlite import Fetched

from tether.agent_run import record_run
from tether.agent_trace_model import RunKind
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.model_selection import AgentModelConfig
from tether.notification_delivery import TriggerDispatchResult
from tether.pi_errors import PiRuntimeError
from tether.pi_process import PiSpawner, PiSpawnRequest, spawn_pi_runtime
from tether.pi_runtime import PiRuntime
from tether.pi_turn_events import MessageSettled, ModelTurnStarted
from tether.structured_logging import Logger
from tether.system_prompt import system_prompt_for
from tether.tool_runtime import SessionRegistry
from tether.trigger_store import ScheduledOccurrence
from tether.triggers import DEFAULT_BACKOFF_BASE, DEFAULT_MAX_ATTEMPTS, TriggerService


class Clock(Protocol):
    """A source of the current instant, injectable for controlled-clock tests."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        """Return the current UTC instant."""
        return datetime.now(UTC)


class TriggerDispatchPort(Protocol):
    """Execute an occurrence and independently deliver a stored prompt answer."""

    async def dispatch(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> TriggerDispatchResult: ...

    async def deliver_prompt_push(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EphemeralPiConfig:
    """Wiring for spawning an ephemeral pi to run one agent-prompt trigger."""

    session_registry: SessionRegistry
    session_root: Path
    tool_base_url: str
    tool_secret: str
    model: AgentModelConfig | None = None
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    pi_binary: Path | None = None
    event_timeout_seconds: float = 60.0
    load_tether_tools: bool = True
    trace_recorder: AgentTraceRecorder | None = None
    run_kind: RunKind = "scheduled"


class EphemeralPiPromptRunner:
    """Runs an agent-prompt trigger in a throwaway pi process.

    Each call spawns a fresh, closed-tool-world pi, sends the prompt, drains its
    RPC event stream to the final assistant message, and shuts the process down.
    Nothing about the run is persisted in pi — the result is returned to the
    caller, which is the host's source of truth.
    """

    def __init__(
        self,
        config: EphemeralPiConfig,
        *,
        spawn: PiSpawner[PiRuntime] = PiRuntime.spawn,
    ) -> None:
        self.config: EphemeralPiConfig = config
        self._spawn: PiSpawner[PiRuntime] = spawn

    async def run(self, prompt: str) -> str:
        """Spawn pi, run `prompt`, and return its final assistant text."""
        # Ephemeral lifecycle: a fresh session id is generated per run, names
        # its own throwaway session dir, and the runtime is torn down in the
        # `finally` below rather than kept alive across calls.
        runtime, session_id = await spawn_pi_runtime(
            PiSpawnRequest(
                extra_extension_paths=self.config.extra_extension_paths,
                load_tether_tools=self.config.load_tether_tools,
                pi_binary=self.config.pi_binary,
                session_dir=lambda sid: self.config.session_root / sid,
                session_id=None,
                system_prompt=system_prompt_for(self.config.run_kind),
                tool_base_url=self.config.tool_base_url,
                tool_secret=self.config.tool_secret,
            ),
            session_registry=self.config.session_registry,
            spawn=self._spawn,
        )
        try:
            with record_run(
                self.config.trace_recorder,
                session_id=session_id,
                kind=self.config.run_kind,
                prompt=prompt,
            ):
                return await self._drive(runtime, prompt, session_id)
        finally:
            await runtime.shutdown()

    async def _drive(self, runtime: PiRuntime, prompt: str, session_id: str) -> str:
        """Set the model, send the prompt, and drain pi to its final text."""
        if self.config.model is not None:
            await runtime.apply_model(self.config.model)
        response = await runtime.client.request("prompt", message=prompt)
        if response.get("success") is not True:
            message = "agent prompt was rejected by pi"
            raise PiRuntimeError(message)
        return await self._collect_final_text(runtime, session_id)

    async def _collect_final_text(self, runtime: PiRuntime, session_id: str) -> str:
        """Drain the typed turn stream, keeping the last settled assistant text."""
        recorder = self.config.trace_recorder
        final_text = ""
        async for turn_event in runtime.stream_turn(
            wait_seconds=self.config.event_timeout_seconds
        ):
            match turn_event:
                case ModelTurnStarted() if recorder is not None:
                    recorder.record_model_turn(session_id=session_id)
                case MessageSettled(text=text) if text:
                    final_text = text
                case _:
                    pass
        return final_text


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Tunables for the scheduler loop."""

    tick_seconds: float = 30.0
    concurrency: int = 4
    claim_limit: int = 32
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base: timedelta = DEFAULT_BACKOFF_BASE


class Scheduler:
    """The in-process tick loop that fires due Scheduled triggers."""

    def __init__(
        self,
        *,
        service: TriggerService,
        dispatcher: TriggerDispatchPort,
        clock: Clock,
        logger: Logger,
        config: SchedulerConfig | None = None,
    ) -> None:
        self.service: TriggerService = service
        self.dispatcher: TriggerDispatchPort = dispatcher
        self.clock: Clock = clock
        self.logger: Logger = logger
        self.config: SchedulerConfig = config or SchedulerConfig()
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self.config.concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._stopping: bool = False

    async def tick(self) -> list[ScheduledOccurrence[Fetched]]:
        """Claim every due trigger and launch a dispatch task for each.

        Returns the claimed triggers so a controlled-clock test can assert what a
        single tick picked up. Dispatch runs in the background behind the
        concurrency semaphore; await `drain` to settle the launched tasks.
        """
        if self._stopping:
            return []
        now = self.clock.now()
        claimed = await self.service.claim_due(now, limit=self.config.claim_limit)
        for occurrence in claimed:
            self._launch(self._dispatch(occurrence))
        for occurrence in await self.service.claim_due_push_occurrences(now):
            self._launch(self._deliver_push(occurrence))
        return claimed

    async def repair(self) -> None:
        """Repair durable scheduler state without launching dispatch work."""
        await self.service.release_interrupted_pushes()
        _ = await self.service.repair_occurrences(now=self.clock.now())

    async def dispatch_recovered(self) -> None:
        """Launch recovered work after request-serving dependencies are ready."""
        for occurrence in await self.service.list_recoverable_occurrences():
            self._launch(self._dispatch(occurrence))
        for occurrence in await self.service.claim_due_push_occurrences(
            self.clock.now()
        ):
            self._launch(self._deliver_push(occurrence))

    def _launch(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _dispatch(self, occurrence: ScheduledOccurrence[Fetched]) -> None:
        """Settle one occurrence once; prompt failures never broad-retry."""
        async with self._semaphore:
            occurrence = await self.service.record_running(occurrence)
            if occurrence.status in {"succeeded", "failed", "cancelled"}:
                return
            try:
                result = await self.dispatcher.dispatch(occurrence)
            except Exception as error:
                self.logger.warning(
                    "Scheduled occurrence dispatch failed",
                    occurrence_id=str(occurrence.id),
                    trigger_id=str(occurrence.trigger_id),
                    error=str(error),
                )
                _ = await self.service.record_failure(
                    occurrence,
                    now=self.clock.now(),
                    error=str(error),
                    max_attempts=self.config.max_attempts,
                    backoff_base=self.config.backoff_base,
                )
                return
            settled = await self.service.record_success(
                occurrence,
                now=self.clock.now(),
                answer=result.answer,
                answer_message_id=result.answer_message_id,
            )
            self.logger.info(
                "Scheduled occurrence fired",
                occurrence_id=str(occurrence.id),
                trigger_id=str(occurrence.trigger_id),
                action_kind=occurrence.action_kind,
            )
            if settled.action_kind == "prompt":
                push_occurrence = await self.service.claim_prompt_push(settled.id)
                if push_occurrence is not None:
                    await self.dispatcher.deliver_prompt_push(
                        push_occurrence,
                        now=self.clock.now(),
                    )

    async def _deliver_push(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> None:
        async with self._semaphore:
            await self.dispatcher.deliver_prompt_push(
                occurrence,
                now=self.clock.now(),
            )

    async def drain(self) -> None:
        """Await every in-flight dispatch task (for tests and shutdown)."""
        while self._inflight:
            pending = list(self._inflight)
            _ = await asyncio.gather(*pending, return_exceptions=True)

    def stop_intake(self) -> None:
        """Prevent ticks and reconciliation from launching more dispatch work."""
        self._stopping = True
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Run ticks on the configured interval until cancelled.

        The interval is awaited before each tick (not after), so a process that
        starts and stops quickly never fires a tick whose DB work would race the
        shutdown that closes the connection pool.
        """
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                _ = await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.tick_seconds,
                )
            if self._stopping:
                return
            try:
                _ = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.logger.warning("Scheduler tick failed", error=str(error))

    async def shutdown(self, *, drain_seconds: float = 1.0) -> None:
        """Bound waiter drain so a stuck dispatcher cannot deadlock host exit."""
        self.stop_intake()
        if not self._inflight:
            return
        _, pending = await asyncio.wait(self._inflight, timeout=drain_seconds)
        for task in pending:
            _ = task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
