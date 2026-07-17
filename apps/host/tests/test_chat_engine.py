"""Conversation runtime registry lifecycle tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID, uuid7

from snekql.sqlite import Fetched
from snektest import assert_eq, assert_raises, assert_true, test

from tether.chat_engine import (
    ConversationRuntimeRegistry,
    RuntimeRegistryConfig,
    _RuntimeSlot,
)
from tether.conversations import Conversation
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.pi_runtime import PiRuntime, PiRuntimeError
from tether.system_prompt import CONVERSATION_SYSTEM_PROMPT
from tether.tools import SessionRegistry

from .pi_runtime_fakes import FakePiRuntime as FakeRuntime
from .pi_runtime_fakes import ModelRejectingRuntime, RecordingSpawner


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


_CHEAP_MODEL = AgentModelConfig(
    display_name="Cheap Faux",
    id="cheap",
    model_id="tether-chat-cheap-faux",
    provider="faux",
)


def make_model_registry(
    directory: str, spawner: RecordingSpawner
) -> ConversationRuntimeRegistry:
    """Build a registry whose catalog carries a curated default model."""
    return ConversationRuntimeRegistry(
        RuntimeRegistryConfig(
            extra_extension_paths=(),
            model_catalog=AgentModelCatalog(
                default_model="cheap", models=(_CHEAP_MODEL,)
            ),
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
async def runtime_for_spawns_with_the_conversation_system_prompt() -> None:
    """Every conversation runtime replaces pi's default with the Tether persona."""
    conversation = FakeConversation(uuid7())
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        _ = await registry.runtime_for(cast("Conversation[Fetched]", conversation))

    assert_eq(len(spawner.configs), 1)
    assert_eq(spawner.configs[0].system_prompt, CONVERSATION_SYSTEM_PROMPT)


@test()
async def runtime_for_applies_the_resolved_model_on_spawn() -> None:
    """A freshly spawned runtime adopts the conversation's resolved model."""
    conversation = FakeConversation(uuid7())
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_model_registry(directory, spawner)
        runtime = await registry.runtime_for(
            cast("Conversation[Fetched]", conversation)
        )

    assert_eq(cast("FakeRuntime", runtime).applied_models, [_CHEAP_MODEL])


@test()
async def runtime_for_shuts_down_the_spawn_when_the_model_is_rejected() -> None:
    """A spawn whose model push fails is torn down, not leaked as an orphan."""
    conversation = FakeConversation(uuid7())
    spawner = RecordingSpawner(
        runtime_factory=lambda config: ModelRejectingRuntime(
            session_id=str(config.session_id)
        )
    )
    with TemporaryDirectory() as directory:
        registry = make_model_registry(directory, spawner)
        with assert_raises(PiRuntimeError):
            _ = await registry.runtime_for(cast("Conversation[Fetched]", conversation))

    assert_eq(len(spawner.spawned), 1)
    assert_eq(spawner.spawned[0].shutdown_count, 1)
    assert_eq(registry.current_for(conversation.id), None)


@test()
async def set_model_applies_to_a_live_runtime() -> None:
    """`set_model` forwards the model to the conversation's live runtime."""
    conversation_id = uuid7()
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        live = FakeRuntime(session_id="live")
        registry._runtimes[str(conversation_id)] = _RuntimeSlot(
            last_used=0.0, runtime=cast("PiRuntime", live)
        )

        await registry.set_model(conversation_id, _CHEAP_MODEL)

    assert_eq(live.applied_models, [_CHEAP_MODEL])


@test()
async def set_model_is_a_no_op_without_a_live_runtime() -> None:
    """`set_model` skips silently when no runtime is bound to the conversation."""
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        await registry.set_model(uuid7(), _CHEAP_MODEL)

    assert_eq(len(spawner.spawned), 0)


@test()
async def discard_tears_down_and_forgets_the_runtime() -> None:
    """`discard` shuts down the bound runtime and drops it from the registry."""
    conversation_id = uuid7()
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        live = FakeRuntime(session_id="live")
        registry._runtimes[str(conversation_id)] = _RuntimeSlot(
            last_used=0.0, runtime=cast("PiRuntime", live)
        )

        await registry.discard(conversation_id)

        assert_eq(live.shutdown_count, 1)
        assert_eq(registry.current_for(conversation_id), None)


@test()
async def discard_is_a_no_op_without_a_live_runtime() -> None:
    """`discard` tolerates a conversation with no bound runtime."""
    spawner = RecordingSpawner()
    with TemporaryDirectory() as directory:
        registry = make_registry(directory, spawner)
        await registry.discard(uuid7())

    assert_eq(len(spawner.spawned), 0)


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
