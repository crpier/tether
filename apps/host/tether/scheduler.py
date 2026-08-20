"""In-process Scheduled-trigger scheduler: poll, claim, dispatch, back off.

Durability lives in the loop plus SQLite, with no external broker. Every tick
asks the `TriggerService` for due rows, which are stamped `claimed` before any
dispatch so a row in flight is never picked up twice. Each claimed trigger is
dispatched as an `asyncio` task gated behind a concurrency semaphore
(backpressure); a successful dispatch settles the row (a `once` trigger
completes, a recurring one re-arms) and a failed one is backed off via
`next_attempt_at` for a later retry.

Dispatch itself is a `TriggerDispatcher`: a fixed-message trigger delivers its
payload through Inbox notification channels, while an agent-prompt trigger runs
as a normal turn in the default Conversation. The resulting assistant message
also retains configured Web Push delivery.

The loop takes its time from a `Clock`, so tests drive it with a controlled
clock and a fake dispatcher and assert fire + retry behaviour without sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from snekql.sqlite import Fetched

from tether.agent_run import record_run
from tether.agent_trace_model import RunKind
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.chat_turn import ChatTurnDependencies, run_chat_prompt
from tether.events import EventPublisher, InvalidateEvent
from tether.model_selection import AgentModelConfig
from tether.pi_errors import PiRuntimeError
from tether.pi_process import PiSpawner, PiSpawnRequest, spawn_pi_runtime
from tether.pi_runtime import PiRuntime
from tether.pi_turn_events import MessageSettled, ModelTurnStarted
from tether.structured_logging import Logger
from tether.system_prompt import system_prompt_for
from tether.tool_runtime import SessionRegistry
from tether.trigger_store import ScheduledTrigger
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
    """Dispatch one claimed Scheduled trigger to its delivery pipeline."""

    async def dispatch(self, trigger: ScheduledTrigger[Fetched]) -> None:
        """Resolve and deliver one trigger, propagating delivery defects."""
        ...


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


class _BackgroundChatSink:
    """Discard streaming frames while a scheduled turn settles durably."""

    async def send_json(self, data: Any) -> None:
        """Drop one transient frame; the transcript is refreshed after the turn."""
        _ = data


class ScheduledChatPromptRunner:
    """Run a scheduled prompt through the default Conversation.

    The canonical transcript receives the same user and agent rows as an
    interactive prompt. Connected browsers reload the settled transcript once
    the unattended turn ends.
    """

    def __init__(
        self,
        dependencies: ChatTurnDependencies,
        *,
        event_publisher: EventPublisher,
    ) -> None:
        self.dependencies: ChatTurnDependencies = dependencies
        self.event_publisher: EventPublisher = event_publisher

    async def run(self, prompt: str) -> str:
        """Submit `prompt` to the default chat and return its settled answer."""
        conversation = (
            await self.dependencies.conversation_service.list_conversations()
        )[0]
        try:
            return await run_chat_prompt(
                _BackgroundChatSink(),
                self.dependencies,
                conversation_id=conversation.id,
                content=prompt,
            )
        finally:
            await self.event_publisher.publish(
                InvalidateEvent(keys=["messages", "conversations"])
            )


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

    async def tick(self) -> list[ScheduledTrigger[Fetched]]:
        """Claim every due trigger and launch a dispatch task for each.

        Returns the claimed triggers so a controlled-clock test can assert what a
        single tick picked up. Dispatch runs in the background behind the
        concurrency semaphore; await `drain` to settle the launched tasks.
        """
        now = self.clock.now()
        claimed = await self.service.claim_due(now, limit=self.config.claim_limit)
        for trigger in claimed:
            task = asyncio.create_task(self._dispatch(trigger))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        return claimed

    async def _dispatch(self, trigger: ScheduledTrigger[Fetched]) -> None:
        """Dispatch one claimed trigger, settling its outcome to the service."""
        async with self._semaphore:
            try:
                await self.dispatcher.dispatch(trigger)
            except Exception as error:
                self.logger.warning(
                    "Scheduled trigger dispatch failed",
                    trigger_id=str(trigger.id),
                    error=str(error),
                )
                _ = await self.service.record_failure(
                    trigger,
                    now=self.clock.now(),
                    error=str(error),
                    max_attempts=self.config.max_attempts,
                    backoff_base=self.config.backoff_base,
                )
            else:
                self.logger.info(
                    "Scheduled trigger fired",
                    trigger_id=str(trigger.id),
                    action_kind=trigger.action_kind,
                )
                _ = await self.service.record_success(trigger, now=self.clock.now())

    async def drain(self) -> None:
        """Await every in-flight dispatch task (for tests and shutdown)."""
        while self._inflight:
            pending = list(self._inflight)
            _ = await asyncio.gather(*pending, return_exceptions=True)

    async def run_forever(self) -> None:
        """Run ticks on the configured interval until cancelled.

        The interval is awaited before each tick (not after), so a process that
        starts and stops quickly never fires a tick whose DB work would race the
        shutdown that closes the connection pool.
        """
        while True:
            await asyncio.sleep(self.config.tick_seconds)
            try:
                _ = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.logger.warning("Scheduler tick failed", error=str(error))

    async def shutdown(self) -> None:
        """Stop accepting work and wait for in-flight dispatches to settle."""
        with contextlib.suppress(Exception):
            await self.drain()
