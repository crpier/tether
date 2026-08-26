"""Lifespan composition for the headless deterministic capability host."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Literal

from anyio import Path as AsyncPath
from fastapi import FastAPI
from snekql.sqlite import Config, Database

from tether.app_runtime import AppRuntime, install_app_runtime
from tether.bucket_item_search import BucketItemSearchService
from tether.bucket_items import BucketItemService
from tether.health_connect import (
    HealthConnectIngestion,
    HealthConnectTelemetry,
    HealthEpisodeSummarizer,
    create_health_connect_schema,
)
from tether.host_config import AppConfig
from tether.host_schema import create_host_schema
from tether.logging_config import QUIET_LOGGERS, configure_logging
from tether.telemetry_config import configure_telemetry
from tether.telemetry_model import TelemetrySettings
from tether.todos import TodoService
from tether.triage import TriageService

HOST_QUIET_LOGGERS = (*QUIET_LOGGERS, "aiosqlite", "snekql", "httpcore2")
"""Dependency loggers whose debug chatter obscures host operations."""

type DatabasePath = Path | Literal[":memory:"]


def _database_config(path: str | Path) -> DatabasePath:
    """Preserve SQLite's in-memory sentinel while normalizing file paths."""
    if str(path) == ":memory:":
        return ":memory:"
    return Path(path)


async def _prepare_parent(path: DatabasePath) -> None:
    """Create a file-backed database's parent without touching in-memory stores."""
    if path != ":memory:":
        await AsyncPath(Path(path).parent).mkdir(parents=True, exist_ok=True)


async def _stop_tasks(tasks: tuple[asyncio.Task[None], ...]) -> None:
    """Cancel deterministic workers before their databases unwind."""
    for task in tasks:
        _ = task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def app_lifespan(
    *, config: AppConfig, telemetry_settings: TelemetrySettings
) -> Callable[[FastAPI], AbstractAsyncContextManager[None, bool | None]]:
    """Create a lifespan owning databases, retained services, and one worker."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Acquire the complete runtime and unwind it in reverse order."""
        main_path = _database_config(config.database_path)
        if config.telemetry_database_path is not None:
            telemetry_path = _database_config(config.telemetry_database_path)
        elif main_path == ":memory:":
            telemetry_path = ":memory:"
        else:
            telemetry_path = main_path.parent / "telemetry.sqlite3"
        await _prepare_parent(main_path)
        await _prepare_parent(telemetry_path)
        logger = configure_logging(
            config.logging_level,
            log_file=config.log_file,
            quiet_loggers=HOST_QUIET_LOGGERS,
        )
        telemetry = configure_telemetry(telemetry_settings)
        async with contextlib.AsyncExitStack() as resources:
            _ = resources.callback(telemetry.shutdown)
            database = await resources.enter_async_context(
                await Database.initialize(Config(database=main_path))
            )
            telemetry_database = await resources.enter_async_context(
                await Database.initialize(Config(database=telemetry_path))
            )
            await create_host_schema(database)
            await create_health_connect_schema(telemetry_database)
            tasks = (
                asyncio.create_task(
                    HealthEpisodeSummarizer(telemetry_database).sweep_forever(
                        interval_seconds=config.health_episode_sweep_seconds,
                        logger=logger,
                    ),
                    name="health-episode-sweep",
                ),
            )
            _ = resources.push_async_callback(_stop_tasks, tasks)
            install_app_runtime(
                app,
                AppRuntime(
                    bucket_item_search_service=BucketItemSearchService(
                        database=database, tracer=telemetry.tracer
                    ),
                    bucket_item_service=BucketItemService(
                        database=database, tracer=telemetry.tracer
                    ),
                    health_connect_ingestion=HealthConnectIngestion(telemetry_database),
                    health_connect_telemetry=HealthConnectTelemetry.from_database(
                        telemetry_database
                    ),
                    logger=logger,
                    tasks=tasks,
                    telemetry=telemetry,
                    todo_service=TodoService(
                        database=database, tracer=telemetry.tracer
                    ),
                    triage_service=TriageService(database=database),
                ),
            )
            yield

    return lifespan


__all__ = ["HOST_QUIET_LOGGERS", "app_lifespan"]
