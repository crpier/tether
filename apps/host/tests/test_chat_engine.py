"""Conversation runtime registry lifecycle tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID, uuid7

from snekql.sqlite import Fetched
from snektest import assert_eq, assert_true, test

from tether.chat_engine import (
    ConversationRuntimeRegistry,
    RuntimeRegistryConfig,
    _RuntimeSlot,
)
from tether.conversations import Conversation
from tether.model_selection import AgentModelCatalog
from tether.pi_runtime import PiRuntime, PiRuntimeConfig
from tether.tools import SessionRegistry


class FakeProcess:
    """Minimal process state used by registry health checks."""

    def __init__(self) -> None:
        self.returncode: int | None = None


class FakeRuntime:
    """Runtime double recording shutdown and its bound pi session id."""

    def __init__(self, session_id: str = "session") -> None:
        self.process: FakeProcess = FakeProcess()
        self.session_id: str = session_id
        self.shutdown_count: int = 0

    async def shutdown(self) -> None:
        """Mark the runtime as stopped."""
        self.shutdown_count += 1
        self.process.returncode = 0


class RecordingSpawner:
    """Spawn seam that returns fakes bound to the requested pi session id."""

    def __init__(self) -> None:
        self.spawned: list[FakeRuntime] = []

    async def __call__(
        self, config: PiRuntimeConfig, *, session_registry: SessionRegistry
    ) -> PiRuntime:
        """Return a fake runtime tagged with the spawned session id."""
        _ = session_registry
        runtime = FakeRuntime(session_id=str(config.session_id))
        self.spawned.append(runtime)
        return cast("PiRuntime", runtime)


class FakeConversation:
    """Conversation stand-in carrying just the fields the registry reads."""

    def __init__(self, pi_session_id: UUID) -> None:
        self.id: UUID = uuid7()
        self.pi_session_id: UUID = pi_session_id
        self.selected_model: str | None = None


def make_registry(
    directory: str, spawner: RecordingSpawner
) -> ConversationRuntimeRegistry:
    """Build a registry wired to a spawn seam and a fixed clock."""
    return ConversationRuntimeRegistry(
        RuntimeRegistryConfig(
            extra_extension_paths=(),
            model_catalog=AgentModelCatalog(default_model=None, models=()),
            idle_seconds=30 * 60,
            pi_binary=None,
            session_registry=SessionRegistry(),
            session_root=Path(directory),
            tool_base_url="http://127.0.0.1:9",
            tool_secret="secret",
        ),
        now=lambda: 0.0,
        spawn=spawner,
    )


@test()
async def runtime_for_respawns_when_the_pi_session_rotated() -> None:
    """A live runtime bound to a stale session is torn down and replaced."""
    session_id = uuid7()
    conversation = FakeConversation(session_id)
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        stale = FakeRuntime(session_id="stale-session")
        registry._runtimes[str(conversation.id)] = _RuntimeSlot(
            last_used=0.0, runtime=cast("PiRuntime", stale)
        )

        runtime = await registry.runtime_for(
            cast("Conversation[Fetched]", conversation)
        )

    assert_eq(stale.shutdown_count, 1)
    assert_eq(cast("FakeRuntime", runtime).session_id, str(session_id))
    assert_true(runtime is not cast("PiRuntime", stale))


@test()
async def runtime_for_reuses_a_live_runtime_on_the_current_session() -> None:
    """A live runtime bound to the current session is returned without spawning."""
    session_id = uuid7()
    conversation = FakeConversation(session_id)
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        live = FakeRuntime(session_id=str(session_id))
        registry._runtimes[str(conversation.id)] = _RuntimeSlot(
            last_used=0.0, runtime=cast("PiRuntime", live)
        )

        runtime = await registry.runtime_for(
            cast("Conversation[Fetched]", conversation)
        )

    assert_true(runtime is cast("PiRuntime", live))
    assert_eq(live.shutdown_count, 0)
    assert_eq(len(spawner.spawned), 0)


@test()
async def registry_reaps_runtimes_after_the_idle_ttl() -> None:
    """Conversation pi processes are torn down after the configured idle window."""
    now = 0.0
    with TemporaryDirectory() as directory:
        registry = ConversationRuntimeRegistry(
            RuntimeRegistryConfig(
                extra_extension_paths=(),
                model_catalog=AgentModelCatalog(default_model=None, models=()),
                idle_seconds=30 * 60,
                pi_binary=None,
                session_registry=SessionRegistry(),
                session_root=Path(directory),
                tool_base_url="http://127.0.0.1:9",
                tool_secret="secret",
            ),
            now=lambda: now,
        )
        runtime = FakeRuntime()
        registry._runtimes["conversation"] = _RuntimeSlot(
            last_used=0.0, runtime=cast("PiRuntime", runtime)
        )

        now = 30 * 60 - 1
        await registry.reap_idle()
        still_live = registry.current_for("conversation")

        now = 60 * 60
        await registry.reap_idle()

    assert_eq(still_live is not None, True)
    assert_eq(runtime.shutdown_count, 1)
    assert_eq(registry.current_for("conversation"), None)
