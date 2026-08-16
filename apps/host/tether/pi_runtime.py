"""Lifecycle owner for a spawned pi RPC process."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Self, cast
from uuid import UUID

import structlog

from tether.model_selection import AgentModelConfig
from tether.pi_errors import PiRuntimeError
from tether.pi_process import (
    BUNDLED_PI_SKILL_NAMES,
    PiRuntimeConfig,
    build_pi_spawn_command,
    build_pi_spawn_environment,
)
from tether.pi_rpc import PiRpcClient
from tether.pi_turn_events import AgentEnded, TurnEvent, decode_turn_event
from tether.tools import SessionRegistry

_SHUTDOWN_TIMEOUT_SECONDS = 5.0
"""Time to wait for pi to exit before escalating termination."""

_UUID_VERSION_7 = 7
"""UUID version emitted by pi for new session identities."""

_logger = structlog.stdlib.get_logger("tether.pi_runtime")
"""Operational diagnostics for pi startup and resource confirmation."""


class PiRuntime:
    """A spawned pi RPC process registered with the host session registry."""

    def __init__(
        self,
        *,
        client: PiRpcClient,
        process: asyncio.subprocess.Process,
        session_id: str,
        session_registry: SessionRegistry,
    ) -> None:
        self.client: PiRpcClient = client
        self.process: asyncio.subprocess.Process = process
        self.session_id: str = session_id
        self.session_registry: SessionRegistry = session_registry
        self.session_uuid: UUID = UUID(session_id)
        self.loaded_skills: tuple[str, ...] = ()
        self.skills_confirmed: bool = False
        self._shutdown_complete: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.shutdown()

    @classmethod
    async def spawn(
        cls,
        config: PiRuntimeConfig,
        *,
        session_registry: SessionRegistry,
    ) -> Self:
        """Start pi, confirm its identity and resources, then register it."""
        session_id = config.session_id or str(uuid.uuid7())
        process = await asyncio.create_subprocess_exec(
            *build_pi_spawn_command(config, session_id),
            cwd=config.cwd,
            env=build_pi_spawn_environment(config, session_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            _ = await process.wait()
            message = "pi RPC stdio pipes were not created"
            raise PiRuntimeError(message)
        client = PiRpcClient(reader=process.stdout, writer=process.stdin)
        await client.start()
        runtime = cls(
            client=client,
            process=process,
            session_id=session_id,
            session_registry=session_registry,
        )
        try:
            resolved_session_id = await runtime._resolve_session_id()
            runtime._confirm_session_id(resolved_session_id)
            await runtime.confirm_loaded_skills()
            session_registry.register(resolved_session_id)
        except Exception:
            await runtime.shutdown()
            raise
        return runtime

    async def health(self) -> bool:
        """Return true when the process responds successfully to `get_state`."""
        if self.process.returncode is not None:
            return False
        response = await self.client.request("get_state")
        return response.get("success") is True

    async def apply_model(self, model: AgentModelConfig) -> None:
        """Select the resolved model and optional thinking level for later turns."""
        response = await self.client.request(
            "set_model",
            provider=model.provider,
            modelId=model.model_id,
        )
        if response.get("success") is not True:
            message = f"pi rejected set_model: {response.get('error', 'unknown error')}"
            raise PiRuntimeError(message)
        if model.thinking_level is None:
            return
        thinking_response = await self.client.request(
            "set_thinking_level",
            level=model.thinking_level,
        )
        if thinking_response.get("success") is not True:
            message = (
                "pi rejected set_thinking_level: "
                f"{thinking_response.get('error', 'unknown error')}"
            )
            raise PiRuntimeError(message)

    async def confirm_loaded_skills(self) -> None:
        """Confirm release-managed skills without exposing resource metadata."""
        try:
            response = await self.client.request("get_commands")
        except Exception as error:
            _logger.warning(
                "Pi skill confirmation unavailable",
                error_type=type(error).__name__,
                session_id=self.session_id,
            )
            return
        data = response.get("data")
        if response.get("success") is not True or not isinstance(data, dict):
            _logger.warning(
                "Pi skill confirmation malformed", session_id=self.session_id
            )
            return
        commands = cast("dict[str, object]", data).get("commands")
        if not isinstance(commands, list):
            _logger.warning(
                "Pi skill confirmation malformed", session_id=self.session_id
            )
            return
        loaded_skill_names: set[str] = set()
        for raw_command in cast("list[object]", commands):
            if not isinstance(raw_command, dict):
                continue
            command = cast("dict[str, object]", raw_command)
            name = command.get("name")
            if command.get("source") != "skill" or not isinstance(name, str):
                continue
            skill_name = name.removeprefix("skill:")
            if name == skill_name or not skill_name.replace("-", "").isalnum():
                continue
            if skill_name in BUNDLED_PI_SKILL_NAMES:
                loaded_skill_names.add(skill_name)
        self.loaded_skills = tuple(
            name for name in BUNDLED_PI_SKILL_NAMES if name in loaded_skill_names
        )
        self.skills_confirmed = True
        if len(self.loaded_skills) != len(BUNDLED_PI_SKILL_NAMES):
            _logger.warning(
                "Bundled pi skills missing",
                expected_count=len(BUNDLED_PI_SKILL_NAMES),
                loaded_count=len(self.loaded_skills),
                session_id=self.session_id,
            )

    def drain_events(self) -> int:
        """Discard pending events left over from a previous turn."""
        return self.client.drain_events()

    async def next_event(
        self,
        event_type: str | None = None,
        *,
        wait_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Read queued records until one matching `event_type` arrives."""
        while True:
            try:
                event = await asyncio.wait_for(
                    self.client.events.get(),
                    timeout=wait_seconds,
                )
            except TimeoutError:
                message = f"no pi event within {wait_seconds:g}s"
                raise TimeoutError(message) from None
            if event_type is None or event.get("type") == event_type:
                return event

    async def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]:
        """Yield typed events until pi closes the turn with `AgentEnded`."""
        while True:
            turn_event = decode_turn_event(
                await self.next_event(wait_seconds=wait_seconds)
            )
            if turn_event is None:
                continue
            yield turn_event
            if isinstance(turn_event, AgentEnded):
                return

    async def shutdown(self) -> None:
        """Close RPC, stop the process, and unregister its session exactly once."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        await self.client.close()
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self.process.stdin.wait_closed()
        await self._wait_or_terminate()
        self.session_registry.discard(self.session_id)

    async def _resolve_session_id(self) -> str:
        """Fetch pi's own session id from `get_state`."""
        response = await self.client.request("get_state")
        if response.get("success") is not True:
            message = f"pi get_state failed: {response.get('error', 'unknown error')}"
            raise PiRuntimeError(message)
        data = response.get("data")
        if not isinstance(data, dict):
            message = "pi get_state response did not include a sessionId"
            raise PiRuntimeError(message)
        session_id = cast("dict[str, object]", data).get("sessionId")
        if not isinstance(session_id, str):
            message = "pi get_state response did not include a sessionId"
            raise PiRuntimeError(message)
        return session_id

    def _confirm_session_id(self, resolved_session_id: str) -> None:
        """Ensure env, command line, and pi-reported identity agree."""
        try:
            resolved_uuid = UUID(resolved_session_id)
        except ValueError as error:
            message = "pi session id is not a UUID"
            raise PiRuntimeError(message) from error
        if resolved_uuid.version != _UUID_VERSION_7:
            message = "pi session id is not UUIDv7"
            raise PiRuntimeError(message)
        if resolved_session_id != self.session_id:
            message = "pi reported a different session id than the host injected"
            raise PiRuntimeError(message)

    async def _wait_or_terminate(self) -> None:
        """Prefer EOF shutdown, then terminate and finally kill a stuck pi."""
        if self.process.returncode is not None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            _ = await asyncio.wait_for(
                self.process.wait(),
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
            return
        self.process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            _ = await asyncio.wait_for(
                self.process.wait(),
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
            return
        self.process.kill()
        _ = await self.process.wait()


__all__ = ["PiRuntime"]
