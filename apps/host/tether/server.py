"""Starlette server for the Tether host: wires the Memory service over HTTP.

>>> # Run the host with `python -m tether`.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from anyio import Path as AsyncPath
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database
from starlette.applications import Starlette

from tether.auth import AppSessionMiddleware, auth_routes
from tether.bucket_items import (
    BucketItemService,
    create_bucket_item_schema,
)
from tether.bucket_routes import bucket_item_routes
from tether.bucket_tools import internal_bucket_tool_routes
from tether.logging import ContextLoggerMiddleware, configure_logging
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.openapi import openapi_routes
from tether.routes import routes
from tether.telemetry import (
    TelemetryExporter,
    TelemetryMiddleware,
    TelemetrySettings,
    configure_telemetry,
)
from tether.tools import SessionRegistry, internal_tool_routes


@dataclass(frozen=True, slots=True)
class AppConfig:
    """In-process configuration for one Starlette app instance.

    ```python
    config = AppConfig(app_password="pw", session_secret="secret")
    assert config.secure_cookies is False
    ```
    """

    app_password: str
    session_secret: str
    database_path: str | Path = Path(".tether/tether.sqlite3")
    kb_root: str | Path = Path(".tether")
    logging_level: str = "INFO"
    secure_cookies: bool = False


class HostSettings(BaseSettings):
    """Environment-backed configuration for the host server process.

    ```python
    settings = HostSettings()  # reads `TETHER_` environment variables
    ```
    """

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    app_password: str = Field(default="", min_length=1)
    session_secret: str = Field(default="", min_length=1)
    database_path: Path = Path(".tether/tether.sqlite3")
    host: str = "127.0.0.1"
    kb_root: Path = Path(".tether")
    logging_level: str = "INFO"
    port: int = 8000
    reload: bool = False
    telemetry_environment: str = "development"
    telemetry_exporter: TelemetryExporter = TelemetryExporter.NONE
    telemetry_service_name: str = "tether-host"
    tool_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def telemetry(self) -> TelemetrySettings:
        """OpenTelemetry settings derived from `TETHER_TELEMETRY_` variables."""
        return TelemetrySettings(
            environment=self.telemetry_environment,
            exporter=self.telemetry_exporter,
            service_name=self.telemetry_service_name,
            service_version="0.1.0",
        )


def _lifespan(
    *,
    database_path: str | Path,
    kb_root: str | Path,
    logging_level: str,
    telemetry_settings: TelemetrySettings,
) -> Callable[[Starlette], AbstractAsyncContextManager[None, bool | None]]:
    """Create lifespan wiring for a configured SQLite DB and KB root."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """Build the Memory service for the app lifetime and close it after."""
        app_logger = configure_logging(logging_level)
        telemetry = configure_telemetry(telemetry_settings)
        app.state.logger = app_logger
        app.state.telemetry = telemetry
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
            await create_bucket_item_schema(db)
            kb_service = KnowledgeBaseService(kb_root=configured_kb_root)
            memory_service = MemoryService(
                database=db,
                kb_service=kb_service,
                tracer=telemetry.tracer,
            )
            await memory_service.regenerate_knowledge_base(logger=app_logger)
            app.state.memory_service = memory_service
            app.state.bucket_item_service = BucketItemService(
                database=db,
                tracer=telemetry.tracer,
            )
            try:
                yield
            finally:
                telemetry.shutdown()

    return lifespan


def create_app(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings | None = None,
    tool_secret: str | None = None,
) -> Starlette:
    """Construct the Starlette application with Memory routes and lifespan wiring.

    The Memory routes are also handed to `openapi_routes` so `/openapi.json`
    and `/docs` describe exactly the API that is mounted. By default, both the
    SQLite database and markdown Knowledge base live under `.tether`.
    """
    api_routes = [*auth_routes, *routes, *bucket_item_routes]
    docs = openapi_routes(api_routes, title="Tether", version="0.1.0")
    configured_telemetry = telemetry_settings or TelemetrySettings()
    app = Starlette(
        routes=[
            *api_routes,
            *internal_tool_routes(),
            *internal_bucket_tool_routes(),
            *docs,
        ],
        lifespan=_lifespan(
            database_path=config.database_path,
            kb_root=config.kb_root,
            logging_level=config.logging_level,
            telemetry_settings=configured_telemetry,
        ),
    )
    app.state.app_password = config.app_password
    app.state.secure_cookies = config.secure_cookies
    app.state.session_registry = SessionRegistry()
    app.state.session_secret = config.session_secret
    app.state.tool_secret = (
        tool_secret if tool_secret is not None else secrets.token_urlsafe(32)
    )
    app.add_middleware(ContextLoggerMiddleware)
    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(
        AppSessionMiddleware,
        secure=config.secure_cookies,
        session_secret=config.session_secret,
    )
    return app


def create_app_from_environment() -> Starlette:
    """Create the ASGI app from `TETHER_` environment variables.

    ```python
    app = create_app_from_environment()
    ```
    """
    settings = HostSettings()
    return create_app(
        config=AppConfig(
            app_password=settings.app_password,
            database_path=settings.database_path,
            kb_root=settings.kb_root,
            logging_level=settings.logging_level,
            secure_cookies=settings.telemetry_environment == "production",
            session_secret=settings.session_secret,
        ),
        telemetry_settings=settings.telemetry,
        tool_secret=settings.tool_secret,
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
