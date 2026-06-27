"""Conversation runtime registry lifecycle tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from snektest import assert_eq, test

from tether.chat_engine import (
    ConversationRuntimeRegistry,
    RuntimeRegistryConfig,
    _RuntimeSlot,
)
from tether.model_selection import AgentModelCatalog
from tether.pi_runtime import PiRuntime
from tether.tools import SessionRegistry


class FakeProcess:
    """Minimal process state used by registry health checks."""

    def __init__(self) -> None:
        self.returncode: int | None = None


class FakeRuntime:
    """Runtime double recording shutdown."""

    def __init__(self) -> None:
        self.process: FakeProcess = FakeProcess()
        self.shutdown_count: int = 0

    async def shutdown(self) -> None:
        """Mark the runtime as stopped."""
        self.shutdown_count += 1
        self.process.returncode = 0


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
