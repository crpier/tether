"""Shared `PiRuntime` test doubles for the chat and scheduler spawn seams.

Both `ConversationRuntimeRegistry` (persistent, chat) and
`EphemeralPiPromptRunner` (ephemeral, scheduled/recall) take a `PiSpawner`
seam (`tether.pi_runtime.PiSpawner`) as an injectable collaborator instead of
calling `PiRuntime.spawn` directly. This module is the one fake process/
runtime/spawner trio both call sites' tests share, so a real subprocess never
needs to be spawned in a unit test and the fake stays type-compatible with the
real `PiRuntime` (no `setattr` module-global monkeypatching required).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from typing import Any, cast

from tether.model_selection import AgentModelConfig
from tether.pi_runtime import (
    AgentEnded,
    PiRuntime,
    PiRuntimeConfig,
    PiRuntimeError,
    TurnEvent,
)
from tether.tools import SessionRegistry


class FakeProcess:
    """Minimal process state used by registry/runtime health checks."""

    def __init__(self) -> None:
        self.returncode: int | None = None


class FakeRpcClient:
    """A canned pi RPC client: accepts or rejects the `prompt` request."""

    def __init__(self, *, accepts_prompt: bool = True) -> None:
        self.accepts_prompt: bool = accepts_prompt
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def request(self, method: str, **params: object) -> dict[str, Any]:
        """Log the call and answer every RPC, rejecting `prompt` when scripted to."""
        self.requests.append((method, params))
        if method == "prompt" and not self.accepts_prompt:
            return {"success": False, "error": "scripted rejection"}
        return {"success": True}


class FakePiRuntime:
    """A pi runtime double: scripted RPC answers, no subprocess, no events.

    Covers both call sites' needs: `process`/`session_id`/`apply_model`/
    `shutdown` for the persistent chat registry, and `client`/`stream_turn`
    for the ephemeral prompt runner.
    """

    def __init__(
        self,
        *,
        session_id: str = "session",
        accepts_prompt: bool = True,
        goes_silent: bool = False,
        turn_events: Sequence[TurnEvent] | None = None,
    ) -> None:
        self.process: FakeProcess = FakeProcess()
        self.session_id: str = session_id
        self.client: FakeRpcClient = FakeRpcClient(accepts_prompt=accepts_prompt)
        self.shutdown_count: int = 0
        self.applied_models: list[AgentModelConfig] = []
        self._goes_silent: bool = goes_silent
        self._turn_events: Sequence[TurnEvent] = (
            turn_events if turn_events is not None else (AgentEnded(),)
        )

    async def apply_model(self, model: AgentModelConfig) -> None:
        """Record the model this runtime was asked to adopt."""
        self.applied_models.append(model)

    async def shutdown(self) -> None:
        """Mark the runtime as stopped."""
        self.shutdown_count += 1
        self.process.returncode = 0

    async def stream_turn(
        self, *, wait_seconds: float = 5.0
    ) -> AsyncGenerator[TurnEvent]:
        """Yield the scripted turn events, or simulate pi going silent."""
        _ = wait_seconds
        if self._goes_silent:
            raise TimeoutError("no pi event within the wait")
            yield AgentEnded()  # unreachable: keeps this an async generator
        for turn_event in self._turn_events:
            yield turn_event


class ModelRejectingRuntime(FakePiRuntime):
    """Runtime double whose pi rejects every model switch."""

    async def apply_model(self, model: AgentModelConfig) -> None:
        """Refuse the switch the way a live pi does on a failed `set_model`."""
        _ = model
        message = "pi rejected set_model"
        raise PiRuntimeError(message)


class RecordingSpawner:
    """A type-compatible `PiSpawner` double recording every spawn config.

    Hands out either a fixed prepared runtime (the common case for a test that
    scripts one exchange) or one freshly built per call via `runtime_factory`
    (for tests, like the chat registry's, that assert on the session id each
    spawned runtime was tagged with).
    """

    def __init__(
        self,
        *,
        runtime: FakePiRuntime | None = None,
        runtime_factory: Callable[[PiRuntimeConfig], FakePiRuntime] | None = None,
    ) -> None:
        self.configs: list[PiRuntimeConfig] = []
        self.spawned: list[FakePiRuntime] = []
        self._runtime: FakePiRuntime | None = runtime
        self._runtime_factory: Callable[[PiRuntimeConfig], FakePiRuntime] | None = (
            runtime_factory
        )

    async def __call__(
        self, config: PiRuntimeConfig, *, session_registry: SessionRegistry
    ) -> PiRuntime:
        """Record the config and hand out the prepared or freshly built fake."""
        _ = session_registry
        self.configs.append(config)
        if self._runtime is not None:
            runtime = self._runtime
        elif self._runtime_factory is not None:
            runtime = self._runtime_factory(config)
        else:
            runtime = FakePiRuntime(session_id=str(config.session_id))
        self.spawned.append(runtime)
        return cast("PiRuntime", runtime)
