"""In-process Scheduled-trigger scheduler: poll, claim, dispatch, back off.

Durability lives in the loop plus SQLite, with no external broker. Every tick
asks the `TriggerService` for due rows, which are stamped `claimed` before any
dispatch so a row in flight is never picked up twice. Each claimed trigger is
dispatched as an `asyncio` task gated behind a concurrency semaphore
(backpressure); a successful dispatch settles the row (a `once` trigger
completes, a recurring one re-arms) and a failed one is backed off via
`next_attempt_at` for a later retry.

Dispatch itself is a `TriggerDispatcher`: a fixed-message trigger delivers its
payload verbatim, while an agent-prompt trigger runs the payload through an
ephemeral pi process and delivers the result. Delivery is a `TriggerNotifier`,
which by default fans a `NotifyEvent` out over the in-process event hub to every
connected browser.

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
from typing import Any, Protocol, cast
from uuid import uuid7

from anyio import Path as AsyncPath
from snekql.sqlite import Fetched

from tether.events import EventPublisher, NotifyEvent
from tether.logging import Logger
from tether.model_selection import AgentModelConfig
from tether.pi_runtime import PiRuntime, PiRuntimeConfig, PiRuntimeError
from tether.tools import SessionRegistry
from tether.triggers import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_ATTEMPTS,
    ScheduledTrigger,
    TriggerService,
)


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


class TriggerNotifier(Protocol):
    """Delivers a fired trigger's message to the user."""

    async def deliver(
        self,
        *,
        trigger: ScheduledTrigger[Fetched],
        message: str,
    ) -> None:
        """Deliver one fired trigger's message."""
        ...


class EventNotifier:
    """Delivers fired triggers as `NotifyEvent`s over the in-process event hub.

    This is the WebSocket half of capture → resurface: the frame reaches every
    browser currently connected to the host. (Real Web Push to a closed tab is a
    deferred follow-up; subscriptions are stored separately by the push service.)
    """

    def __init__(self, event_publisher: EventPublisher) -> None:
        self.event_publisher: EventPublisher = event_publisher

    async def deliver(
        self,
        *,
        trigger: ScheduledTrigger[Fetched],
        message: str,
    ) -> None:
        """Publish a notification frame for one fired trigger."""
        await self.event_publisher.publish(
            NotifyEvent(body=message, trigger_id=str(trigger.id))
        )


class AgentPromptRunner(Protocol):
    """Runs an agent prompt to completion and returns the delivered text."""

    async def run(self, prompt: str) -> str:
        """Run `prompt` through the agent and return its final message."""
        ...


def _assistant_message_text(message: object) -> str:
    """Extract the displayed text from a settled pi assistant message."""
    if not isinstance(message, dict):
        return ""
    message_data = cast("dict[str, Any]", message)
    if message_data.get("role") != "assistant":
        return ""
    content = message_data.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for raw_item in cast("list[object]", content):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(cast("str", item["text"]))
    return "".join(chunks)


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


class EphemeralPiPromptRunner:
    """Runs an agent-prompt trigger in a throwaway pi process.

    Each call spawns a fresh, closed-tool-world pi, sends the prompt, drains its
    RPC event stream to the final assistant message, and shuts the process down.
    Nothing about the run is persisted in pi — the result is returned to the
    caller, which is the host's source of truth.
    """

    def __init__(self, config: EphemeralPiConfig) -> None:
        self.config: EphemeralPiConfig = config

    async def run(self, prompt: str) -> str:
        """Spawn pi, run `prompt`, and return its final assistant text."""
        session_id = str(uuid7())
        session_dir = self.config.session_root / session_id
        await AsyncPath(session_dir).mkdir(parents=True, exist_ok=True)
        runtime = await PiRuntime.spawn(
            PiRuntimeConfig(
                extra_extension_paths=self.config.extra_extension_paths,
                pi_binary=self.config.pi_binary,
                session_dir=session_dir,
                session_id=session_id,
                tool_base_url=self.config.tool_base_url,
                tool_secret=self.config.tool_secret,
            ),
            session_registry=self.config.session_registry,
        )
        try:
            if self.config.model is not None:
                _ = await runtime.client.request(
                    "set_model",
                    provider=self.config.model.provider,
                    modelId=self.config.model.model_id,
                )
            response = await runtime.client.request("prompt", message=prompt)
            if response.get("success") is not True:
                message = "agent prompt was rejected by pi"
                raise PiRuntimeError(message)
            return await self._collect_final_text(runtime)
        finally:
            await runtime.shutdown()

    async def _collect_final_text(self, runtime: PiRuntime) -> str:
        """Drain pi events to the turn's end, keeping the last assistant text."""
        final_text = ""
        while True:
            event = await runtime.next_event(
                wait_seconds=self.config.event_timeout_seconds
            )
            match event.get("type"):
                case "message_end":
                    text = _assistant_message_text(event.get("message"))
                    if text:
                        final_text = text
                case "agent_end":
                    return final_text
                case _:
                    pass


class TriggerDispatcher:
    """Turns a fired trigger into a delivered message.

    A `message` action delivers its payload verbatim; a `prompt` action runs the
    payload through the agent and delivers the result. Failures propagate so the
    scheduler can back the occurrence off and retry.
    """

    def __init__(
        self,
        *,
        notifier: TriggerNotifier,
        agent_runner: AgentPromptRunner,
    ) -> None:
        self.notifier: TriggerNotifier = notifier
        self.agent_runner: AgentPromptRunner = agent_runner

    async def dispatch(self, trigger: ScheduledTrigger[Fetched]) -> None:
        """Deliver one fired trigger, running the agent first if needed."""
        if trigger.action_kind == "message":
            message = trigger.payload
        else:
            message = await self.agent_runner.run(trigger.payload)
        await self.notifier.deliver(trigger=trigger, message=message)


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
        dispatcher: TriggerDispatcher,
        clock: Clock,
        logger: Logger,
        config: SchedulerConfig | None = None,
    ) -> None:
        self.service: TriggerService = service
        self.dispatcher: TriggerDispatcher = dispatcher
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
