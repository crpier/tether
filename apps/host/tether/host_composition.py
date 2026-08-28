"""Application lifespan and final typed runtime assembly."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from tether.app_runtime import AppRuntime, install_app_runtime
from tether.email_evidence import EmailEvidenceService
from tether.evidence import EvidenceResolver
from tether.gmail import (
    GoogleGmailAuthService,
    HttpGmailTransport,
    ReauthorizableGmailClient,
)
from tether.health_connect import (
    HealthConnectEvidenceResolver,
    HealthConnectIngestion,
    HealthConnectTelemetry,
    create_health_connect_schema,
)
from tether.host_config import AppConfig
from tether.host_resources import (
    HostBootstrap,
    acquire_host_resources,
)
from tether.host_schema import create_host_schema
from tether.ingestion_composition import (
    IngestionDependencies,
    compose_ingestion,
)
from tether.search_projection.embeddings import Embedder
from tether.service_composition import CoreServices, compose_core_services
from tether.telemetry_model import TelemetrySettings
from tether.transcripts.contracts import AsyncClosable


class ServingReadyMiddleware:
    """Signal when the composed ASGI app receives its first live request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        application = scope.get("app")
        if scope["type"] in {"http", "websocket"} and isinstance(application, FastAPI):
            serving_ready = getattr(application.state, "serving_ready", None)
            if isinstance(serving_ready, asyncio.Event):
                serving_ready.set()
        await self.app(scope, receive, send)


@dataclass(frozen=True, slots=True)
class _RuntimeDependencies:
    """Configuration and process dependencies for final runtime assembly."""

    bootstrap: HostBootstrap
    config: AppConfig
    embedder: Embedder | None
    telemetry_settings: TelemetrySettings


async def _compose_app_runtime(
    app: FastAPI,
    dependencies: _RuntimeDependencies,
    *,
    resources: contextlib.AsyncExitStack,
) -> CoreServices:
    """Build and install the complete request-serving dependency graph."""
    host = await acquire_host_resources(
        config=dependencies.config,
        resources=resources,
        telemetry_settings=dependencies.telemetry_settings,
    )
    await create_host_schema(host.database)
    await create_health_connect_schema(host.telemetry_database)
    core = await compose_core_services(
        bootstrap=dependencies.bootstrap,
        config=dependencies.config,
        embedder=dependencies.embedder,
        host=host,
        resources=resources,
    )
    ingestion_resources = await resources.enter_async_context(
        contextlib.AsyncExitStack()
    )
    _ = resources.push_async_callback(host.ingestion_lifecycle.stop)
    gmail_client = ReauthorizableGmailClient()
    if dependencies.config.gmail_transport is not None:
        gmail_client.connect(dependencies.config.gmail_transport)

    async def _activate_gmail_client() -> None:
        if dependencies.config.gmail_oauth_config is None:
            return
        gmail_client.connect(HttpGmailTransport(dependencies.config.gmail_oauth_config))

    gmail_auth_service = GoogleGmailAuthService(
        dependencies.config.gmail_auth_backend,
        on_authorized=_activate_gmail_client,
    )
    email_evidence_service = EmailEvidenceService(host.database)
    youtube = await compose_ingestion(
        IngestionDependencies(
            bootstrap=dependencies.bootstrap,
            config=dependencies.config,
            database=host.database,
            event_hub=core.event_hub,
            ingestion_lifecycle=host.ingestion_lifecycle,
            kb_root=host.kb_root,
            logger=host.logger,
            model_catalog=core.model_catalog,
            todo_service=core.todo_service,
            tracer=host.telemetry.tracer,
            trigger_service=core.trigger_service,
            youtube_search=core.youtube_search,
            gmail_client=gmail_client,
            gmail_auth_service=gmail_auth_service,
        ),
        resources=ingestion_resources,
    )
    install_app_runtime(
        app,
        AppRuntime(
            app_password=dependencies.config.app_password,
            artifact_service=core.artifact_service,
            attachment_service=core.attachment_service,
            bucket_item_search_service=core.bucket_item_search_service,
            bucket_item_service=core.bucket_item_service,
            conversation_runtime_registry=core.conversation_runtime_registry,
            conversation_service=core.conversation_service,
            conversation_turn_queue=core.conversation_turn_queue,
            conversation_turns=core.conversation_turns,
            event_hub=core.event_hub,
            evidence_resolver=EvidenceResolver(
                host.database,
                HealthConnectEvidenceResolver(host.telemetry_database),
                email_evidence_service,
            ),
            health_connect_ingestion=HealthConnectIngestion(host.telemetry_database),
            health_connect_telemetry=HealthConnectTelemetry.from_database(
                host.telemetry_database
            ),
            health_distillation_service=core.health_distillation_service,
            health_moment_service=core.health_moment_service,
            health_plan_service=core.health_plan_service,
            ingestion_lifecycle=host.ingestion_lifecycle,
            kosync_auth=core.kosync_auth,
            kosync_service=core.kosync_service,
            logger=host.logger,
            memory_workspace_service=core.memory_workspace_service,
            model_catalog=core.model_catalog,
            notification_service=core.notification_service,
            panel_service=core.panel_service,
            product_observation_service=core.product_observation_service,
            provider_auth_service=core.provider_auth_service,
            public_origin=dependencies.config.public_origin,
            gmail_client=gmail_client,
            gmail_auth_service=gmail_auth_service,
            email_evidence_service=email_evidence_service,
            push_service=core.push_service,
            dreaming_enabled=dependencies.config.dreaming_enabled,
            recall_service=core.recall_service,
            search_provider=core.search_provider,
            secure_cookies=dependencies.config.secure_cookies,
            session_registry=dependencies.bootstrap.session_registry,
            dreaming_service=core.dreaming_service,
            session_secret=dependencies.config.session_secret,
            stt_client=dependencies.bootstrap.stt_client,
            telemetry=host.telemetry,
            todo_service=core.todo_service,
            tts_client=dependencies.bootstrap.tts_client,
            tool_secret=dependencies.bootstrap.tool_secret,
            trace_recorder=dependencies.bootstrap.trace_recorder,
            triage_service=core.triage_service,
            trigger_service=core.trigger_service,
            vapid_public_key=dependencies.config.vapid_public_key,
            youtube_auth_service=youtube.auth_service,
            youtube_service=youtube.service,
        ),
    )
    return core


async def _dispatch_when_tool_http_is_ready(
    core: CoreServices,
    *,
    serving_ready: asyncio.Event,
    tool_base_url: str,
) -> None:
    """Keep recovered pi work behind the host's serving socket readiness."""
    parsed_url = urlsplit(tool_base_url)
    host = parsed_url.hostname or "127.0.0.1"
    with contextlib.suppress(ValueError):
        if ip_address(host).is_unspecified:
            host = "127.0.0.1"
    port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)

    async def wait_for_socket() -> None:
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=0.2,
                )
            except TimeoutError, OSError:
                await asyncio.sleep(0.05)
                continue
            _ = reader
            writer.close()
            await writer.wait_closed()
            return

    readiness_tasks = {
        asyncio.create_task(serving_ready.wait()),
        asyncio.create_task(wait_for_socket()),
    }
    try:
        _ = await asyncio.wait(
            readiness_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in readiness_tasks:
            if not task.done():
                _ = task.cancel()
        for task in readiness_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _ = await core.conversation_turns.dispatch_recovered()
    await core.scheduler.dispatch_recovered()
    await core.scheduler.run_forever()


async def _stop_recovered_dispatch(
    core: CoreServices,
    task: asyncio.Task[None],
) -> None:
    """Stop readiness and scheduler loops before database resources unwind."""
    core.scheduler.stop_intake()
    if task.done():
        if not task.cancelled():
            _ = task.exception()
        return
    try:
        _ = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
    except TimeoutError:
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def app_lifespan(
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    telemetry_settings: TelemetrySettings,
    embedder: Embedder | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None, bool | None]]:
    """Create the application lifespan over one explicitly owned resource graph."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Compose the runtime, serve requests, then unwind resources in order."""
        async with contextlib.AsyncExitStack() as resources:
            serving_ready = asyncio.Event()
            app.state.serving_ready = serving_ready
            for configured_resource in (
                config.gmail_transport,
                config.transcript_provider,
            ):
                if isinstance(configured_resource, AsyncClosable):
                    _ = resources.push_async_callback(configured_resource.aclose)
            core = await _compose_app_runtime(
                app,
                _RuntimeDependencies(
                    bootstrap=bootstrap,
                    config=config,
                    embedder=embedder,
                    telemetry_settings=telemetry_settings,
                ),
                resources=resources,
            )
            recovered_dispatch = asyncio.create_task(
                _dispatch_when_tool_http_is_ready(
                    core,
                    serving_ready=serving_ready,
                    tool_base_url=config.tool_base_url,
                )
            )
            _ = resources.push_async_callback(
                _stop_recovered_dispatch,
                core,
                recovered_dispatch,
            )
            yield

    return lifespan
