"""FastAPI server for the Tether host: wires the Memory service over HTTP.

>>> # Run the host with `python -m tether`.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_404_NOT_FOUND
from starlette.types import Scope
from uvicorn.config import WSProtocolType

from tether.agent_trace import AgentTraceRecorder
from tether.artifact_tools import internal_artifact_tool_routes
from tether.auth import AppSessionMiddleware
from tether.bucket_tools import internal_bucket_tool_routes
from tether.chat_ws import websocket_routes
from tether.conversation_history_tools import (
    internal_conversation_history_tool_routes,
)
from tether.embeddings import Embedder, FakeEmbedder, FastEmbedder
from tether.gmail import (
    GmailTransport,
)
from tether.gmail_oauth import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    HttpGmailTransport,
)
from tether.health_connect_tools import internal_health_connect_tool_routes
from tether.host_composition import HOST_QUIET_LOGGERS, app_lifespan
from tether.host_config import AppConfig, HostSettings
from tether.kosync_routes import kosync_protocol_routes
from tether.kosync_tools import internal_kosync_tool_routes
from tether.local_dependencies import LocalProviderAuthBackend, LocalSttTransport
from tether.model_selection import AgentModelConfig
from tether.openapi_export import public_api_router
from tether.panel_tools import internal_panel_tool_routes
from tether.proposal_tools import internal_proposal_tool_routes
from tether.recall_tools import internal_recall_tool_routes
from tether.search_tools import (
    HttpTavilyTransport,
    SearchProvider,
    TavilySearchProvider,
    internal_search_tool_routes,
)
from tether.structured_logging import (
    ContextLoggerMiddleware,
    configure_logging,
)
from tether.stt import HttpSttTransport, SttClient
from tether.telemetry import (
    TelemetryMiddleware,
    TelemetrySettings,
)
from tether.todo_tools import internal_todo_tool_routes
from tether.tools import SessionRegistry, internal_tool_routes
from tether.trace_routes import trace_routes
from tether.transcripts.acquisition import (
    TranscriptAcquisitionConfig,
)
from tether.transcripts.source_composition import (
    SupadataSourceConfig,
    TranscriptLibrarySourceConfig,
    TranscriptProviderConfig,
    build_configured_transcript_provider,
)
from tether.transcripts.worker import TranscriptSyncConfig
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.youtube import (
    YouTubeApi,
)
from tether.youtube_oauth import OAuthConfig, OAuthYouTubeApi
from tether.youtube_tools import internal_youtube_tool_routes


def _resolve_stt_client(config: AppConfig) -> SttClient:
    """Build the voice-capability STT client from config.

    An injected `stt_client` (tests, a custom wiring) wins outright. Otherwise a
    live client is built from `stt_api_key`/`stt_base_url`/`stt_model`. STT is a
    required host dependency (ADR 0018) — `app.state.stt_client` is always set,
    never `None`.
    """
    if config.stt_client is not None:
        return config.stt_client
    return SttClient(
        transport=HttpSttTransport(config.stt_api_key, base_url=config.stt_base_url),
        model=config.stt_model,
    )


class _SpaStaticFiles(StaticFiles):
    """Serve the built SPA, falling back to `index.html` for client routes.

    The web app does client-side routing, so a GET for a path that isn't a real
    asset must return the SPA shell (`index.html`) instead of a bare 404 —
    otherwise refreshing or deep-linking a client route breaks. This is the
    conventional single-page-app contract; the API/WS/docs routes are matched
    ahead of this catch-all mount, so only genuinely unmatched paths reach here.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Resolve a static asset, serving the SPA shell when none matches.

        In `html` mode `StaticFiles` *raises* `HTTPException(404)` for an
        unmatched path rather than returning a 404 response, so the fallback is
        handled in both the raised and returned form.
        """
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != HTTP_404_NOT_FOUND:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == HTTP_404_NOT_FOUND:
            return await super().get_response("index.html", scope)
        return response


def _spa_mount(web_dist: str | Path) -> Mount | None:
    """Build the SPA catch-all mount when a built `web_dist` directory exists.

    Returns `None` when no build is configured or present (the dev/test default),
    so the host runs API + WS only and the root path stays unhandled.
    """
    dist = Path(web_dist)
    if not dist.is_dir():
        return None
    return Mount("/", app=_SpaStaticFiles(directory=dist, html=True), name="spa")


def create_app(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings | None = None,
    tool_secret: str | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    """Construct the FastAPI application with Memory routes and lifespan wiring.

    FastAPI derives `/openapi.json` and `/docs` from the mounted public router.
    By default, both the
    SQLite database and markdown Knowledge base live under `.tether`. `embedder`
    defaults to the in-host `FastEmbedder`; tests pass a `FakeEmbedder` to drive
    the search path without downloading a model.
    """
    configured_telemetry = telemetry_settings or TelemetrySettings()
    spa_mount = _spa_mount(config.web_dist) if config.web_dist is not None else None
    app = FastAPI(
        title="Tether",
        version="0.1.0",
        lifespan=app_lifespan(
            config=config,
            telemetry_settings=configured_telemetry,
            embedder=embedder,
        ),
    )
    app.include_router(public_api_router())
    app.router.routes.extend(
        [
            *trace_routes(),
            *internal_tool_routes(),
            *internal_bucket_tool_routes(),
            *internal_todo_tool_routes(),
            *internal_artifact_tool_routes(),
            *internal_triage_tool_routes(),
            *internal_youtube_tool_routes(),
            *internal_search_tool_routes(),
            *internal_trigger_tool_routes(),
            *internal_recall_tool_routes(),
            *internal_conversation_history_tool_routes(),
            *internal_panel_tool_routes(),
            *internal_kosync_tool_routes(),
            *internal_health_connect_tool_routes(),
            *internal_proposal_tool_routes(),
            *(
                kosync_protocol_routes()
                if config.kosync_enabled
                and config.kosync_username
                and config.kosync_userkey
                else []
            ),
            *websocket_routes,
            # The SPA catch-all mounts at "/", so it must come last.
            *([spa_mount] if spa_mount is not None else []),
        ]
    )
    app.state.app_password = config.app_password
    app.state.secure_cookies = config.secure_cookies
    app.state.vapid_public_key = config.vapid_public_key
    app.state.session_registry = SessionRegistry()
    app.state.trace_recorder = AgentTraceRecorder()
    app.state.session_secret = config.session_secret
    app.state.tool_secret = (
        tool_secret if tool_secret is not None else secrets.token_urlsafe(32)
    )
    app.state.stt_client = _resolve_stt_client(config)
    app.add_middleware(ContextLoggerMiddleware)
    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(
        AppSessionMiddleware,
        secure=config.secure_cookies,
        session_secret=config.session_secret,
        api_token=config.api_token,
    )
    return app


def _build_search_provider(settings: HostSettings) -> SearchProvider | None:
    """Build Tavily only when both the explicit flag and API key are configured."""
    if not settings.search_enabled or not settings.search_api_key:
        return None
    return TavilySearchProvider(
        HttpTavilyTransport(settings.search_api_key),
        min_request_interval_seconds=settings.search_min_request_interval_seconds,
    )


def build_configured_youtube_api(settings: HostSettings) -> YouTubeApi | None:
    """Build the OAuth-backed upstream client when a token has been authorized.

    With no cached token, returns `None` so ingestion runs the in-memory fake and
    the background sync stays off — and the Google client libraries are never
    imported, keeping the rest of Tether runnable without them. Once the user has
    run `just youtube-auth`, the token exists and this wires the real client so
    the background ingestion sync activates automatically.
    """
    if not settings.youtube_token_path.exists():
        return None
    return OAuthYouTubeApi.from_config(_youtube_oauth_config(settings))


def _youtube_oauth_config(settings: HostSettings) -> OAuthConfig:
    """Build the shared OAuth config for the YouTube adapters."""
    return OAuthConfig(
        token_path=settings.youtube_token_path,
        client_secret_path=settings.youtube_client_secret_path,
        no_browser=settings.youtube_oauth_no_browser,
    )


def build_configured_gmail_transport(settings: HostSettings) -> GmailTransport | None:
    """Build the OAuth-backed Gmail transport once a token has been authorized.

    With no cached token, returns `None` so the background gate never wires
    (see `_wire_gmail`) and the Google client libraries stay unneeded for a
    default install. Once the user has run `just gmail-auth`, the token
    exists and this wires the real transport so ingestion activates
    automatically on the next host start.
    """
    if not settings.gmail_token_path.exists():
        return None
    return HttpGmailTransport(
        OAuthConfig(
            token_path=settings.gmail_token_path,
            client_secret_path=settings.gmail_client_secret_path,
            scopes=(GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE),
            no_browser=settings.gmail_oauth_no_browser,
        )
    )


def _local_app_config_from_settings(settings: HostSettings) -> AppConfig:
    """Build isolated deterministic wiring without consulting integration config."""
    local_root = settings.local_data_root
    return AppConfig(
        app_password=settings.app_password,
        database_path=local_root / "tether.sqlite3",
        default_model="local",
        ebook_statistics_db_path="",
        extra_extension_paths=(
            Path(__file__).resolve().parents[2] / "agent/src/local-faux.ts",
        ),
        gmail_purge_enabled=False,
        gmail_sync_enabled=False,
        gmail_transport=None,
        kb_root=local_root / "kb",
        kosync_enabled=False,
        log_file=local_root / "logs/host.log",
        logging_level=settings.logging_level,
        model_allowlist=(
            AgentModelConfig(
                display_name="Local deterministic model",
                id="local",
                model_id="tether-local-faux",
                provider="faux",
            ),
        ),
        provider_auth_backend=LocalProviderAuthBackend(),
        readwise_api_key="",
        readwise_reader_sync_enabled=False,
        readwise_sync_enabled=False,
        search_provider=None,
        secure_cookies=False,
        session_secret=settings.session_secret,
        stt_client=SttClient(LocalSttTransport(), model="local"),
        telemetry_database_path=local_root / "telemetry.sqlite3",
        tool_base_url=f"http://{settings.host}:{settings.port}",
        transcript_provider=None,
        transcript_sync_enabled=False,
        vapid_private_key="",
        vapid_public_key="",
        vapid_subject="",
        web_dist=settings.web_dist,
        youtube_api=None,
        youtube_sync_enabled=False,
    )


def _transcript_provider_config(settings: HostSettings) -> TranscriptProviderConfig:
    """Convert flat environment fields into one provider-composition value."""
    languages = tuple(
        language.strip()
        for language in settings.transcript_languages.split(",")
        if language.strip()
    ) or ("en",)
    return TranscriptProviderConfig(
        languages=languages,
        library=TranscriptLibrarySourceConfig(
            enabled=settings.transcript_library_enabled,
            min_request_interval=timedelta(
                seconds=settings.transcript_library_min_request_interval_seconds
            ),
        ),
        supadata=SupadataSourceConfig(
            api_key=settings.supadata_api_key,
            base_url=settings.supadata_base_url,
            enabled=settings.supadata_enabled,
            max_poll_attempts=settings.supadata_max_poll_attempts,
            min_request_interval=timedelta(
                seconds=settings.supadata_min_request_interval_seconds
            ),
            poll_interval=timedelta(seconds=settings.supadata_poll_interval_seconds),
            timeout=timedelta(seconds=settings.supadata_timeout_seconds),
        ),
    )


def _app_config_from_settings(settings: HostSettings) -> AppConfig:
    """Build the `AppConfig` the app factory wires from environment settings.

    Extracted out of `create_app_from_environment` so the settings -> config
    field mapping is unit-testable without spinning up the full ASGI app (which
    needs a YouTube OAuth token on disk to wire the background workers).
    """
    if settings.dependency_profile == "local":
        return _local_app_config_from_settings(settings)
    return AppConfig(
        api_token=settings.api_token,
        app_password=settings.app_password,
        database_path=settings.database_path,
        telemetry_database_path=settings.resolved_telemetry_database_path,
        default_model=settings.default_model,
        ebook_statistics_db_path=settings.ebook_statistics_db_path,
        ebook_statistics_sync_interval_seconds=(
            settings.ebook_statistics_sync_interval_seconds
        ),
        kb_root=settings.kb_root,
        kosync_enabled=settings.kosync_enabled,
        kosync_username=settings.kosync_username,
        kosync_userkey=settings.kosync_userkey,
        logging_level=settings.logging_level,
        log_file=settings.log_file,
        model_allowlist=settings.model_allowlist,
        search_max_uses=settings.search_max_uses,
        search_provider=_build_search_provider(settings),
        readwise_api_key=settings.readwise_api_key,
        readwise_sync_enabled=settings.readwise_sync_enabled,
        readwise_sync_interval_seconds=settings.readwise_sync_interval_seconds,
        readwise_reader_sync_enabled=settings.readwise_reader_sync_enabled,
        readwise_reader_sync_interval_seconds=(
            settings.readwise_reader_sync_interval_seconds
        ),
        gmail_transport=build_configured_gmail_transport(settings),
        gmail_sync_enabled=settings.gmail_sync_enabled,
        gmail_sync_interval_seconds=settings.gmail_sync_interval_seconds,
        gmail_triage_batch_size=settings.gmail_triage_batch_size,
        gmail_purge_enabled=settings.gmail_purge_enabled,
        gmail_purge_interval_seconds=settings.gmail_purge_interval_seconds,
        gmail_purge_chunk_size=settings.gmail_purge_chunk_size,
        secure_cookies=settings.secure_cookies,
        session_secret=settings.session_secret,
        vapid_private_key=settings.vapid_private_key,
        vapid_public_key=settings.vapid_public_key,
        vapid_subject=settings.vapid_subject,
        stt_api_key=settings.stt_api_key,
        stt_base_url=settings.stt_base_url,
        stt_model=settings.stt_model,
        web_dist=settings.web_dist,
        youtube_api=build_configured_youtube_api(settings),
        youtube_likes_rewalk_interval_days=settings.youtube_likes_rewalk_interval_days,
        youtube_likes_drift_alarm_margin=settings.youtube_likes_drift_alarm_margin,
        youtube_sync_enabled=settings.youtube_sync_enabled,
        transcript_acquisition_config=TranscriptAcquisitionConfig(
            block_pause_base=timedelta(
                seconds=settings.transcript_block_pause_base_seconds
            ),
            block_pause_cap=timedelta(
                seconds=settings.transcript_block_pause_cap_seconds
            ),
        ),
        transcript_provider=build_configured_transcript_provider(
            _transcript_provider_config(settings)
        ),
        transcript_sync_config=TranscriptSyncConfig(
            library_requests_per_pass=(
                settings.transcript_library_max_requests_per_pass
            )
        ),
        transcript_sync_enabled=settings.transcript_sync_enabled,
    )


def create_app_from_environment() -> FastAPI:
    """Create the ASGI app from `TETHER_` environment variables.

    ```python
    app = create_app_from_environment()
    ```
    """
    settings = HostSettings()
    return create_app(
        config=_app_config_from_settings(settings),
        telemetry_settings=settings.telemetry,
        tool_secret=settings.tool_secret,
        embedder=(
            FakeEmbedder() if settings.dependency_profile == "local" else FastEmbedder()
        ),
    )


WS_PROTOCOL: WSProtocolType = "websockets-sansio"
"""uvicorn WebSocket protocol implementation used for the `/ws` upgrade.

uvicorn's default `"auto"` resolves to the legacy `websockets` protocol, which
imports the deprecated `websockets.legacy` module. The sansio implementation
serves the same handshake without the deprecation. Keep server and test fixtures
on this value so both run the protocol shipped in production.
"""


def serve(settings: HostSettings | None = None) -> None:
    """Run the host server with uvicorn using environment-backed settings.

    ```python
    serve(HostSettings(reload=True))
    ```
    """
    configured_settings = HostSettings() if settings is None else settings
    _ = configure_logging(
        configured_settings.logging_level,
        log_file=configured_settings.log_file,
        quiet_loggers=HOST_QUIET_LOGGERS,
    )
    uvicorn.run(
        "tether.server:create_app_from_environment",
        factory=True,
        host=configured_settings.host,
        port=configured_settings.port,
        reload=configured_settings.reload,
        ws=WS_PROTOCOL,
        log_config=None,
        access_log=False,
    )


def main() -> None:
    """Console entrypoint for `python -m tether`."""
    serve()
