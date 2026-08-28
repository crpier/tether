"""Host infrastructure acquisition, shared bootstrap, and orderly shutdown."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from anyio import Path as AsyncPath
from snekql.sqlite import Config, Database

from tether.agent_trace_model import RunKind
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.background_runtime import BackgroundRuntime
from tether.host_config import AppConfig
from tether.logging_config import QUIET_LOGGERS, Logger, configure_logging
from tether.model_selection import AgentModelConfig
from tether.scheduler import EphemeralPiConfig
from tether.stt import SttClient
from tether.telemetry_config import configure_telemetry
from tether.telemetry_model import Telemetry, TelemetrySettings
from tether.tool_runtime import SessionRegistry
from tether.tts import TtsClient

HOST_QUIET_LOGGERS = (*QUIET_LOGGERS, "aiosqlite", "snekql", "httpcore2")
"""Dependency loggers whose debug chatter obscures host application events."""


@dataclass(frozen=True, slots=True)
class HostBootstrap:
    """Process-local dependencies created before application startup."""

    session_registry: SessionRegistry
    stt_client: SttClient
    tool_secret: str
    tts_client: TtsClient
    trace_recorder: AgentTraceRecorder


@dataclass(frozen=True, slots=True)
class HostResources:
    """Infrastructure owned for one application lifetime."""

    background_runtime: BackgroundRuntime
    database: Database
    kb_root: Path
    logger: Logger
    telemetry: Telemetry
    telemetry_database: Database


@asynccontextmanager
async def _open_databases(
    config: AppConfig,
) -> AsyncGenerator[tuple[Database, Database]]:
    """Open independent main and telemetry handles for the application lifetime."""
    database_config = (
        ":memory:"
        if str(config.database_path) == ":memory:"
        else Path(config.database_path)
    )
    telemetry_database_path = config.telemetry_database_path
    if telemetry_database_path is None:
        telemetry_database_path = (
            ":memory:"
            if database_config == ":memory:"
            else database_config.parent / "telemetry.sqlite3"
        )
    telemetry_database_config = (
        ":memory:"
        if str(telemetry_database_path) == ":memory:"
        else Path(telemetry_database_path)
    )
    for configured_database in (database_config, telemetry_database_config):
        if configured_database != ":memory:":
            await AsyncPath(configured_database.parent).mkdir(
                parents=True, exist_ok=True
            )
    async with contextlib.AsyncExitStack() as database_stack:
        main_database = await database_stack.enter_async_context(
            await Database.initialize(backend=Config(database=database_config))
        )
        telemetry_database = await database_stack.enter_async_context(
            await Database.initialize(
                backend=Config(database=telemetry_database_config)
            )
        )
        yield main_database, telemetry_database


async def acquire_host_resources(
    *,
    config: AppConfig,
    resources: contextlib.AsyncExitStack,
    telemetry_settings: TelemetrySettings,
) -> HostResources:
    """Acquire host infrastructure and register cleanup beside each resource."""
    logger = configure_logging(
        config.logging_level,
        log_file=config.log_file,
        quiet_loggers=HOST_QUIET_LOGGERS,
    )
    telemetry = configure_telemetry(telemetry_settings)
    _ = resources.callback(telemetry.shutdown)
    kb_root = Path(config.kb_root)
    await AsyncPath(kb_root).mkdir(parents=True, exist_ok=True)
    await AsyncPath(kb_root / "memory").mkdir(parents=True, exist_ok=True)
    database, telemetry_database = await resources.enter_async_context(
        _open_databases(config)
    )
    return HostResources(
        background_runtime=BackgroundRuntime(logger),
        database=database,
        kb_root=kb_root,
        logger=logger,
        telemetry=telemetry,
        telemetry_database=telemetry_database,
    )


def ephemeral_pi_config(
    bootstrap: HostBootstrap,
    *,
    config: AppConfig,
    kb_root: Path,
    run_kind: RunKind,
    model: AgentModelConfig | None,
) -> EphemeralPiConfig:
    """Build the wiring shared by every ephemeral pi runner."""
    session_root = (
        Path(config.pi_session_root)
        if config.pi_session_root is not None
        else kb_root / "pi-sessions"
    )
    return EphemeralPiConfig(
        session_registry=bootstrap.session_registry,
        session_root=session_root / run_kind,
        tool_base_url=config.tool_base_url,
        tool_secret=bootstrap.tool_secret,
        model=model,
        extra_extension_paths=config.extra_extension_paths,
        pi_binary=config.pi_binary,
        trace_recorder=bootstrap.trace_recorder,
        run_kind=run_kind,
    )
