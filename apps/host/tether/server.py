"""Starlette server for the Tether host: wires the Memory service over HTTP.

>>> app = create_app()
>>> # uvicorn tether.server:app
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import uvicorn
from anyio import Path as AsyncPath
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database
from starlette.applications import Starlette

from tether.logging import ContextLoggerMiddleware, Logger, configure_logging
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.openapi import openapi_routes
from tether.routes import routes


class HostSettings(BaseSettings):
    """Environment-backed configuration for the host server process.

    ```python
    settings = HostSettings()  # reads `TETHER_` environment variables
    ```
    """

    model_config = SettingsConfigDict(env_prefix="TETHER_")

    database_path: Path = Path(".tether/tether.sqlite3")
    host: str = "127.0.0.1"
    kb_root: Path = Path(".tether")
    logging_level: str = "INFO"
    port: int = 8000
    reload: bool = False


def _lifespan(
    *, database_path: str | Path, kb_root: str | Path
) -> Callable[[Starlette], AbstractAsyncContextManager[None, bool | None]]:
    """Create lifespan wiring for a configured SQLite DB and KB root."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """Build the Memory service for the app lifetime and close it after."""
        configured_kb_root = Path(kb_root)
        await AsyncPath(configured_kb_root).mkdir(parents=True, exist_ok=True)
        database_name = str(database_path)
        database_config = (
            ":memory:" if database_name == ":memory:" else Path(database_path)
        )
        if database_config != ":memory:":
            await AsyncPath(database_config.parent).mkdir(
                parents=True,
                exist_ok=True,
            )
        async with await Database.initialize(
            backend=Config(database=database_config),
        ) as db:
            await create_memory_schema(db)
            kb_service = KnowledgeBaseService(kb_root=configured_kb_root)
            memory_service = MemoryService(database=db, kb_service=kb_service)
            await memory_service.regenerate_knowledge_base()
            app.state.memory_service = memory_service
            yield

    return lifespan


def create_app(
    *,
    database_path: str | Path = Path(".tether/tether.sqlite3"),
    kb_root: str | Path = Path(".tether"),
    base_logger: Logger | None = None,
) -> Starlette:
    """Construct the Starlette application with Memory routes and lifespan wiring.

    The Memory routes are also handed to `openapi_routes` so `/openapi.json`
    and `/docs` describe exactly the API that is mounted. By default, both the
    SQLite database and markdown Knowledge base live under `.tether`.
    """
    docs = openapi_routes(routes, title="Tether", version="0.1.0")
    app = Starlette(
        routes=[*routes, *docs],
        lifespan=_lifespan(database_path=database_path, kb_root=kb_root),
    )
    if base_logger is not None:
        app.add_middleware(ContextLoggerMiddleware, base_logger=base_logger)
    return app


def create_app_from_environment() -> Starlette:
    """Create the ASGI app from `TETHER_` environment variables.

    ```python
    app = create_app_from_environment()
    ```
    """
    settings = HostSettings()
    return create_app(
        database_path=settings.database_path,
        kb_root=settings.kb_root,
        base_logger=configure_logging(settings.logging_level),
    )


def serve(settings: HostSettings | None = None) -> None:
    """Run the host server with uvicorn using environment-backed settings.

    ```python
    serve(HostSettings(reload=True))
    ```
    """
    configured_settings = HostSettings() if settings is None else settings
    _ = configure_logging(configured_settings.logging_level)
    uvicorn.run(
        "tether.server:create_app_from_environment",
        factory=True,
        host=configured_settings.host,
        port=configured_settings.port,
        reload=configured_settings.reload,
        log_config=None,
        access_log=False,
    )


def main() -> None:
    """Console entrypoint for `python -m tether`."""
    serve()


app = create_app()
"""Module-level ASGI app for `uvicorn tether.server:app`."""
