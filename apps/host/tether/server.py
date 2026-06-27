"""Starlette server for the Tether host: wires the Memory service over HTTP.

>>> # Run the host with `python -m tether`.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import uvicorn
from anyio import Path as AsyncPath
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database
from starlette.applications import Starlette

from tether.auth import AppSessionMiddleware
from tether.bucket_items import (
    BucketItemService,
    create_bucket_item_schema,
)
from tether.bucket_tools import internal_bucket_tool_routes
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.chat_ws import websocket_routes
from tether.conversations import ConversationService, create_conversation_schema
from tether.events import EventHub
from tether.logging import ContextLoggerMiddleware, Logger, configure_logging
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.openapi import openapi_routes
from tether.openapi_export import public_api_routes
from tether.push import PushService, create_push_schema
from tether.recall import (
    PiStudyItemGenerator,
    RecallService,
    StudyItemGenerator,
    create_recall_schema,
)
from tether.recall_tools import internal_recall_tool_routes
from tether.review import ReviewService
from tether.scheduler import (
    EphemeralPiConfig,
    EphemeralPiPromptRunner,
    EventNotifier,
    Scheduler,
    SchedulerConfig,
    SystemClock,
    TriggerDispatcher,
)
from tether.telemetry import (
    Telemetry,
    TelemetryExporter,
    TelemetryMiddleware,
    TelemetrySettings,
    configure_telemetry,
)
from tether.tools import SessionRegistry, internal_tool_routes
from tether.triage import TriageService
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.triggers import TriggerService, create_trigger_schema
from tether.youtube import (
    InMemoryYouTubeApi,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeService,
    create_youtube_schema,
)
from tether.youtube_tools import internal_youtube_tool_routes


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
    default_model: str | None = None
    default_model_id: str | None = None
    default_model_provider: str | None = None
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    kb_root: str | Path = Path(".tether")
    logging_level: str = "INFO"
    model_allowlist: Sequence[AgentModelConfig] = field(default_factory=tuple)
    pi_binary: Path | None = None
    youtube_api: YouTubeApi | None = None
    youtube_quota_limit: int = 10_000
    pi_idle_seconds: float = 30 * 60
    pi_session_root: str | Path | None = None
    scheduler_concurrency: int = 4
    scheduler_tick_seconds: float = 30.0
    secure_cookies: bool = False
    study_item_generator: StudyItemGenerator | None = None
    tool_base_url: str = "http://127.0.0.1:8000"


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
    model_allowlist: tuple[AgentModelConfig, ...] = ()
    default_model: str | None = None
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


async def _create_schemas(db: Database) -> None:
    """Apply every domain's ordered migrations on an initialized database."""
    await create_memory_schema(db)
    await create_bucket_item_schema(db)
    await create_conversation_schema(db)
    await create_youtube_schema(db)
    await create_trigger_schema(db)
    await create_push_schema(db)
    await create_recall_schema(db)


def _build_scheduler(
    app: Starlette,
    *,
    config: AppConfig,
    trigger_service: TriggerService,
    kb_root: Path,
) -> Scheduler:
    """Wire the Scheduled-trigger scheduler over its dispatch collaborators.

    Agent-prompt triggers spawn ephemeral pi processes under a dedicated session
    root; fixed-message triggers never touch pi. Delivery goes out over the
    in-process event hub as `notify` frames. Shared collaborators (event hub,
    model catalog, session registry, tool secret, logger) are read from the
    already-populated `app.state`.
    """
    model_catalog = cast("AgentModelCatalog", app.state.model_catalog)
    session_root = (
        Path(config.pi_session_root)
        if config.pi_session_root is not None
        else kb_root / "pi-sessions"
    )
    prompt_runner = EphemeralPiPromptRunner(
        EphemeralPiConfig(
            session_registry=app.state.session_registry,
            session_root=session_root / "scheduled",
            tool_base_url=config.tool_base_url,
            tool_secret=app.state.tool_secret,
            model=model_catalog.default_config,
            extra_extension_paths=config.extra_extension_paths,
            pi_binary=config.pi_binary,
        )
    )
    return Scheduler(
        service=trigger_service,
        dispatcher=TriggerDispatcher(
            notifier=EventNotifier(app.state.event_hub),
            agent_runner=prompt_runner,
        ),
        clock=SystemClock(),
        logger=cast("Logger", app.state.logger),
        config=SchedulerConfig(
            tick_seconds=config.scheduler_tick_seconds,
            concurrency=config.scheduler_concurrency,
        ),
    )


def _build_recall_service(
    app: Starlette,
    *,
    config: AppConfig,
    database: Database,
    memory_service: MemoryService,
    kb_root: Path,
) -> RecallService:
    """Wire the Recall service over its model-backed study-item generator.

    Distilling a transcript into learnings + prompts is the one model step in
    Recall, so the generator runs an ephemeral pi under a dedicated session root;
    everything else (grading, scheduling, the completion tether) is pure. Shared
    collaborators are read from the already-populated `app.state`.
    """
    model_catalog = cast("AgentModelCatalog", app.state.model_catalog)
    session_root = (
        Path(config.pi_session_root)
        if config.pi_session_root is not None
        else kb_root / "pi-sessions"
    )
    generator: StudyItemGenerator = config.study_item_generator or PiStudyItemGenerator(
        EphemeralPiPromptRunner(
            EphemeralPiConfig(
                session_registry=app.state.session_registry,
                session_root=session_root / "recall",
                tool_base_url=config.tool_base_url,
                tool_secret=app.state.tool_secret,
                model=model_catalog.default_config,
                extra_extension_paths=config.extra_extension_paths,
                pi_binary=config.pi_binary,
            )
        )
    )
    telemetry = cast("Telemetry", app.state.telemetry)
    return RecallService(
        database=database,
        memory_service=memory_service,
        generator=generator,
        event_publisher=cast("EventHub", app.state.event_hub),
        tracer=telemetry.tracer,
    )


def _lifespan(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings,
) -> Callable[[Starlette], AbstractAsyncContextManager[None, bool | None]]:
    """Create lifespan wiring for a configured SQLite DB and KB root."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """Build the Memory service for the app lifetime and close it after."""
        app_logger = configure_logging(config.logging_level)
        telemetry = configure_telemetry(telemetry_settings)
        app.state.logger = app_logger
        app.state.telemetry = telemetry
        configured_kb_root = Path(config.kb_root)
        await AsyncPath(configured_kb_root).mkdir(parents=True, exist_ok=True)
        database_config = (
            ":memory:"
            if str(config.database_path) == ":memory:"
            else Path(config.database_path)
        )
        if database_config != ":memory:":
            await AsyncPath(database_config.parent).mkdir(
                parents=True,
                exist_ok=True,
            )
        async with await Database.initialize(
            backend=Config(database=database_config),
        ) as db:
            await _create_schemas(db)
            model_catalog = (
                AgentModelCatalog(
                    default_model=config.default_model,
                    models=tuple(config.model_allowlist),
                )
                if config.model_allowlist
                else AgentModelCatalog.from_legacy_default(
                    default_model_id=config.default_model_id,
                    default_model_provider=config.default_model_provider,
                )
            )
            app.state.model_catalog = model_catalog
            kb_service = KnowledgeBaseService(kb_root=configured_kb_root)
            event_hub = EventHub()
            app.state.event_hub = event_hub
            memory_service = MemoryService(
                database=db,
                event_publisher=event_hub,
                kb_service=kb_service,
                tracer=telemetry.tracer,
            )
            await memory_service.regenerate_knowledge_base(logger=app_logger)
            app.state.memory_service = memory_service
            app.state.review_service = ReviewService(database=db)
            app.state.triage_service = TriageService(database=db)
            app.state.bucket_item_service = BucketItemService(
                database=db,
                event_publisher=event_hub,
                tracer=telemetry.tracer,
            )
            app.state.youtube_service = YouTubeService(
                database=db,
                client=YouTubeApiClient(
                    config.youtube_api or InMemoryYouTubeApi(),
                    quota_limit=config.youtube_quota_limit,
                ),
                event_publisher=event_hub,
                tracer=telemetry.tracer,
            )
            app.state.recall_service = _build_recall_service(
                app,
                config=config,
                database=db,
                memory_service=memory_service,
                kb_root=configured_kb_root,
            )
            app.state.conversation_service = ConversationService(
                database=db,
                model_catalog=model_catalog,
            )
            runtime_registry = ConversationRuntimeRegistry(
                RuntimeRegistryConfig(
                    model_catalog=model_catalog,
                    extra_extension_paths=config.extra_extension_paths,
                    idle_seconds=config.pi_idle_seconds,
                    pi_binary=config.pi_binary,
                    session_registry=app.state.session_registry,
                    session_root=Path(config.pi_session_root)
                    if config.pi_session_root is not None
                    else configured_kb_root / "pi-sessions",
                    tool_base_url=config.tool_base_url,
                    tool_secret=app.state.tool_secret,
                )
            )
            app.state.conversation_runtime_registry = runtime_registry
            trigger_service = TriggerService(
                database=db,
                event_publisher=event_hub,
                tracer=telemetry.tracer,
            )
            app.state.trigger_service = trigger_service
            app.state.push_service = PushService(
                database=db,
                event_publisher=event_hub,
            )
            scheduler = _build_scheduler(
                app,
                config=config,
                trigger_service=trigger_service,
                kb_root=configured_kb_root,
            )
            app.state.scheduler = scheduler
            idle_reaper = asyncio.create_task(runtime_registry.reap_idle_forever())
            scheduler_task = asyncio.create_task(scheduler.run_forever())
            try:
                yield
            finally:
                _ = idle_reaper.cancel()
                _ = scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await idle_reaper
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler_task
                await scheduler.shutdown()
                await runtime_registry.shutdown_all()
                telemetry.shutdown()

    return lifespan


def create_app(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings | None = None,
    tool_secret: str | None = None,
) -> Starlette:
    """Construct the Starlette application with Memory routes and lifespan wiring.

    The public REST routes are also handed to `openapi_routes` so `/openapi.json`
    and `/docs` describe exactly the API that is mounted. By default, both the
    SQLite database and markdown Knowledge base live under `.tether`.
    """
    api_routes = public_api_routes()
    docs = openapi_routes(api_routes, title="Tether", version="0.1.0")
    configured_telemetry = telemetry_settings or TelemetrySettings()
    app = Starlette(
        routes=[
            *api_routes,
            *internal_tool_routes(),
            *internal_bucket_tool_routes(),
            *internal_triage_tool_routes(),
            *internal_youtube_tool_routes(),
            *internal_trigger_tool_routes(),
            *internal_recall_tool_routes(),
            *websocket_routes,
            *docs,
        ],
        lifespan=_lifespan(
            config=config,
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
            default_model=settings.default_model,
            kb_root=settings.kb_root,
            logging_level=settings.logging_level,
            model_allowlist=settings.model_allowlist,
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
