"""pi runtime binding for host-owned conversations."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol

from anyio import Path as AsyncPath
from snekql.sqlite import Fetched

from tether.conversations import Conversation
from tether.model_selection import AgentModelCatalog, ModelNotAllowedError
from tether.pi_runtime import PiRuntime, PiRuntimeConfig
from tether.tools import SessionRegistry


@dataclass(frozen=True, slots=True)
class RuntimeRegistryConfig:
    """Configuration for spawning conversation-bound pi runtimes."""

    extra_extension_paths: Sequence[Path]
    model_catalog: AgentModelCatalog
    pi_binary: Path | None
    session_registry: SessionRegistry
    session_root: Path
    tool_base_url: str
    tool_secret: str
    idle_seconds: float = 30 * 60


class PiSpawner(Protocol):
    """Spawn seam for pi runtimes, injectable so tests avoid real subprocesses."""

    async def __call__(
        self, config: PiRuntimeConfig, *, session_registry: SessionRegistry
    ) -> PiRuntime:
        """Spawn a pi runtime for `config`, registered with `session_registry`."""
        ...


@dataclass(slots=True)
class _RuntimeSlot:
    """Live runtime plus idle bookkeeping."""

    last_used: float
    runtime: PiRuntime


class ConversationRuntimeRegistry:
    """In-memory binding from a conversation to its live pi runtime."""

    def __init__(
        self,
        config: RuntimeRegistryConfig,
        *,
        now: Callable[[], float] = monotonic,
        spawn: PiSpawner = PiRuntime.spawn,
    ) -> None:
        self.config: RuntimeRegistryConfig = config
        self._now: Callable[[], float] = now
        self._spawn: PiSpawner = spawn
        self._runtimes: dict[str, _RuntimeSlot] = {}

    def current_for(self, conversation_id: object) -> PiRuntime | None:
        """Return a live runtime without spawning a new process."""
        slot = self._runtimes.get(str(conversation_id))
        if slot is None or slot.runtime.process.returncode is not None:
            return None
        slot.last_used = self._now()
        return slot.runtime

    async def runtime_for(self, conversation: Conversation[Fetched]) -> PiRuntime:
        """Return the live runtime for a conversation, spawning lazily."""
        conversation_key = str(conversation.id)
        slot = self._runtimes.get(conversation_key)
        if slot is not None and slot.runtime.process.returncode is None:
            # A live process bound to the conversation's *current* pi session is
            # reusable; one left on a rotated-out session must be torn down so
            # the next turn starts clean instead of reloading stale context.
            if slot.runtime.session_id == str(conversation.pi_session_id):
                slot.last_used = self._now()
                return slot.runtime
            await slot.runtime.shutdown()
            _ = self._runtimes.pop(conversation_key, None)
        session_dir = self.config.session_root / conversation_key
        await AsyncPath(session_dir).mkdir(parents=True, exist_ok=True)
        runtime = await self._spawn(
            PiRuntimeConfig(
                extra_extension_paths=self.config.extra_extension_paths,
                pi_binary=self.config.pi_binary,
                session_dir=session_dir,
                session_id=str(conversation.pi_session_id),
                tool_base_url=self.config.tool_base_url,
                tool_secret=self.config.tool_secret,
            ),
            session_registry=self.config.session_registry,
        )
        try:
            selected_model = self.config.model_catalog.resolve(
                conversation.selected_model
            )
        except ModelNotAllowedError:
            selected_model = self.config.model_catalog.default_config
        if selected_model is not None:
            _ = await runtime.client.request(
                "set_model",
                provider=selected_model.provider,
                modelId=selected_model.model_id,
            )
        self._runtimes[conversation_key] = _RuntimeSlot(
            last_used=self._now(), runtime=runtime
        )
        return runtime

    async def reap_idle(self) -> None:
        """Shutdown runtimes that have been idle longer than the configured TTL."""
        expired: list[_RuntimeSlot] = []
        now = self._now()
        for conversation_key, slot in list(self._runtimes.items()):
            if slot.runtime.process.returncode is not None:
                _ = self._runtimes.pop(conversation_key, None)
                continue
            if now - slot.last_used >= self.config.idle_seconds:
                expired.append(slot)
                _ = self._runtimes.pop(conversation_key, None)
        for slot in expired:
            await slot.runtime.shutdown()

    async def reap_idle_forever(self, *, interval_seconds: float = 60.0) -> None:
        """Periodically reap idle runtimes until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.reap_idle()

    async def shutdown_all(self) -> None:
        """Terminate every live pi runtime owned by this process."""
        slots = list(self._runtimes.values())
        self._runtimes.clear()
        for slot in slots:
            with contextlib.suppress(Exception):
                await slot.runtime.shutdown()
