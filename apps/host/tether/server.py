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
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import uvicorn
from anyio import Path as AsyncPath
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekql.sqlite import Config, Database
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_404_NOT_FOUND
from starlette.types import Scope
from uvicorn.config import WSProtocolType

from tether.agent_trace import AgentTraceRecorder
from tether.auth import AppSessionMiddleware
from tether.bucket_items import (
    BucketItemService,
    create_bucket_item_schema,
)
from tether.bucket_tools import internal_bucket_tool_routes
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.chat_ws import websocket_routes
from tether.conversations import ConversationService, create_conversation_schema
from tether.embeddings import Embedder, FastEmbedder
from tether.events import EventHub
from tether.logging import ContextLoggerMiddleware, Logger, configure_logging
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.memory_search import MemorySearchService
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.notifications import NotificationService, create_notification_schema
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
from tether.reconciler import SearchReconciler
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
from tether.search_index import SearchIndex
from tether.search_meta import SearchMetaService, create_search_meta_schema
from tether.telemetry import (
    Telemetry,
    TelemetryExporter,
    TelemetryMiddleware,
    TelemetrySettings,
    configure_telemetry,
)
from tether.tools import SessionRegistry, internal_tool_routes
from tether.trace_routes import trace_routes
from tether.transcript_index import TranscriptIndex
from tether.transcript_library import YouTubeTranscriptApiProvider
from tether.transcript_reconciler import TranscriptReconciler
from tether.transcript_search import TranscriptSearchService
from tether.transcript_supadata import (
    HttpSupadataTransport,
    SupadataConfig,
    SupadataMode,
    SupadataTranscriptProvider,
    bind_supadata_spend_guard,
)
from tether.triage import TriageService
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.triggers import TriggerService, create_trigger_schema
from tether.youtube import (
    DailyQuota,
    FallbackTranscriptProvider,
    InMemoryYouTubeApi,
    NullTranscriptProvider,
    TranscriptProvider,
    TranscriptSyncConfig,
    TranscriptSyncService,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeService,
    YouTubeSyncConfig,
    YouTubeSyncService,
    create_youtube_schema,
)
from tether.youtube_oauth import (
    CaptionsTranscriptProvider,
    OAuthConfig,
    OAuthYouTubeApi,
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
    log_file: str | Path | None = None
    model_allowlist: Sequence[AgentModelConfig] = field(default_factory=tuple)
    pi_binary: Path | None = None
    youtube_api: YouTubeApi | None = None
    youtube_daily_quota_limit: int = 10_000
    youtube_sync_enabled: bool = True
    youtube_sync_interval_seconds: float = 5 * 60
    youtube_sync_hot_pages: int = 2
    youtube_sync_backfill_pages: int = 1
    youtube_sync_page_size: int = 50
    youtube_likes_cutoff_date: date | None = None
    youtube_api_gate_pause_base_seconds: float = 15 * 60
    youtube_api_gate_pause_cap_seconds: float = 6 * 60 * 60
    transcript_provider: TranscriptProvider | None = None
    transcript_supadata_max_uses: int = 100
    transcript_sync_enabled: bool = True
    transcript_sync_interval_seconds: float = 5 * 60
    transcript_sync_recent_window: int = 50
    transcript_retry_backoff_base_seconds: float = 10 * 60
    transcript_retry_backoff_cap_seconds: float = 6 * 60 * 60
    transcript_block_pause_base_seconds: float = 30 * 60
    transcript_block_pause_cap_seconds: float = 6 * 60 * 60
    pi_idle_seconds: float = 30 * 60
    pi_session_root: str | Path | None = None
    scheduler_concurrency: int = 4
    scheduler_tick_seconds: float = 30.0
    search_reconcile_seconds: float = 5 * 60
    secure_cookies: bool = False
    study_item_generator: StudyItemGenerator | None = None
    tool_base_url: str = "http://127.0.0.1:8000"
    web_dist: Path | None = None


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
    log_file: Path | None = None
    """Optional path to also write logs to, as one JSON object per line, on top
    of the console. Unset in production/docker (the container's stdout is the log
    sink); `just dev` points it at `.tether/logs/host.log` so an agent can read
    back what the app did when a bug is reported (see `docs/development.md`)."""
    model_allowlist: tuple[AgentModelConfig, ...] = ()
    default_model: str | None = None
    port: int = 8000
    reload: bool = False
    secure_cookies: bool = False
    web_dist: Path | None = None
    youtube_token_path: Path = Path(".tether/youtube-oauth-token.json")
    youtube_client_secret_path: Path = Path(".tether/youtube-client-secret.json")
    youtube_oauth_no_browser: bool = False
    youtube_sync_enabled: bool = True
    """Whether the background liked-videos sync runs. On by default; set
    `TETHER_YOUTUBE_SYNC_ENABLED=false` to keep the upstream client wired for
    on-demand use while skipping the eager boot sync (e.g. the fast dev loop,
    where the startup pass otherwise delays the server binding its port)."""
    transcript_sync_enabled: bool = True
    """Whether the background transcript worker runs. On by default; set
    `TETHER_TRANSCRIPT_SYNC_ENABLED=false` to skip the eager boot drain (which
    otherwise fetches per-video transcripts synchronously and delays startup)."""
    transcript_library_enabled: bool = True
    """Whether to compose the `youtube-transcript-api` fallback behind the captions
    provider. Enabled by default; set `TETHER_TRANSCRIPT_LIBRARY_ENABLED=false` to
    run captions-only (e.g. if the host IP keeps getting blocked)."""
    supadata_enabled: bool = False
    """Whether to compose the paid Supadata provider. When enabled (and keyed) it
    becomes the *primary* transcript source: it is the only source that reliably
    transcribes third-party liked videos, so the owner-only captions API and the
    free library trail it as best-effort fallbacks. Off by default and a no-op
    unless `supadata_api_key` is also set, so enabling paid transcription is a
    deliberate, credentialed choice."""
    supadata_api_key: str = ""
    """Supadata API key. Empty (the default) keeps Supadata out of the chain
    entirely, so the default install never spends and stays offline-friendly."""
    supadata_base_url: str = "https://api.supadata.ai/v1"
    """Supadata API root the provider's HTTP transport issues requests against."""
    supadata_timeout_seconds: float = 30.0
    """Per-request HTTP timeout for Supadata submit and poll calls."""
    supadata_poll_interval_seconds: float = 2.0
    """Delay between polls of an in-flight async Supadata transcript job."""
    supadata_max_poll_attempts: int = 10
    """Poll budget for a Supadata async job before the attempt is treated as transient."""
    supadata_mode: SupadataMode = "native"
    """Supadata transcript mode. `native` (the default) fetches an existing caption
    track only — one use per call — so a caption-less video costs one lookup and
    returns unavailable instead of the multi-use AI `generate` path."""
    supadata_max_uses: int = 100
    """Hard cap on total Supadata uses, persisted across restarts. The background
    sweep stops calling Supadata once this many are spent (remaining videos stay
    pending), bounding spend to a limited plan. Raise it after topping up."""
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
    await create_search_meta_schema(db)
    await create_notification_schema(db)


def _resolve_transcript_provider(
    config: AppConfig, api: YouTubeApi, database: Database
) -> TranscriptProvider:
    """Pick the transcript provider and bind its persisted Supadata use cap.

    The on-demand fetch path always needs a provider: prefer the explicitly
    configured one (the composed captions/Supadata chain in production), else reuse
    the upstream fake when it doubles as a `TranscriptProvider` (the in-memory test
    double), else a null provider that reports every video unavailable. The Supadata
    cap is late-bound here — the provider tree is built from settings before the
    database exists — and is a no-op when the chain has no Supadata.
    """
    provider = config.transcript_provider or (
        api if isinstance(api, InMemoryYouTubeApi) else NullTranscriptProvider()
    )
    bind_supadata_spend_guard(
        provider, database, max_uses=config.transcript_supadata_max_uses
    )
    return provider


async def _wire_youtube(
    app: Starlette,
    *,
    config: AppConfig,
    database: Database,
    event_publisher: EventHub,
    transcript_search: TranscriptSearchService | None = None,
) -> list[asyncio.Task[None]]:
    """Wire the YouTube service + likes/transcript background workers onto state.

    All three share one budgeted client over the configured upstream `YouTubeApi`
    (the in-memory fake when none is configured); the likes sync owns liked-list
    traffic, the transcript worker drains transcripts through the
    `TranscriptProvider`, and the service reads only the local ingested corpus.
    Each worker runs an idempotent boot pass plus a periodic loop only when its
    real upstream is configured (a likes client / a transcript provider); the
    returned tasks are those loops. Otherwise no background traffic runs.
    """
    logger = cast("Logger", app.state.logger)
    tracer = cast("Telemetry", app.state.telemetry).tracer
    api = config.youtube_api or InMemoryYouTubeApi()
    client = YouTubeApiClient(
        api,
        DailyQuota(database, limit=config.youtube_daily_quota_limit),
        clock=SystemClock(),
        gate=YouTubeApiGate(
            database,
            config=YouTubeApiGateConfig(
                pause_base=timedelta(
                    seconds=config.youtube_api_gate_pause_base_seconds
                ),
                pause_cap=timedelta(seconds=config.youtube_api_gate_pause_cap_seconds),
            ),
        ),
    )
    provider = _resolve_transcript_provider(config, api, database)
    youtube_service = YouTubeService(
        database=database,
        client=client,
        provider=provider,
        event_publisher=event_publisher,
        tracer=tracer,
    )
    # Late-bind the optional semantic-search collaborator (None when search is
    # disabled, leaving the lexical LIKE fallback in place).
    youtube_service.transcript_search = transcript_search
    app.state.youtube_service = youtube_service
    sync = YouTubeSyncService(
        database=database,
        client=client,
        tracer=tracer,
        config=YouTubeSyncConfig(
            hot_pages=config.youtube_sync_hot_pages,
            backfill_pages=config.youtube_sync_backfill_pages,
            page_size=config.youtube_sync_page_size,
            cutoff_date=config.youtube_likes_cutoff_date,
            # Gate the startup pass on the periodic cadence: a restart within one
            # interval of the last run (this or a prior process) skips re-syncing,
            # so iterating on the host doesn't re-spend the daily YouTube budget.
            min_interval=timedelta(seconds=config.youtube_sync_interval_seconds),
        ),
        event_publisher=event_publisher,
    )
    app.state.youtube_sync = sync
    transcript_sync = TranscriptSyncService(
        database=database,
        client=client,
        provider=provider,
        config=TranscriptSyncConfig(
            recent_window=config.transcript_sync_recent_window,
            backoff_base=timedelta(
                seconds=config.transcript_retry_backoff_base_seconds
            ),
            backoff_cap=timedelta(seconds=config.transcript_retry_backoff_cap_seconds),
            block_pause_base=timedelta(
                seconds=config.transcript_block_pause_base_seconds
            ),
            block_pause_cap=timedelta(
                seconds=config.transcript_block_pause_cap_seconds
            ),
        ),
        event_publisher=event_publisher,
    )
    app.state.transcript_sync = transcript_sync
    tasks: list[asyncio.Task[None]] = []
    # Boot passes run off the startup critical path so the lifespan completes and
    # uvicorn binds its port immediately (#122); a slow first sync no longer hangs
    # startup. Each worker's `<...>_boot_done` barrier is set once its boot pass
    # finishes (or is skipped), so callers — a readiness probe, and tests that
    # depend on the boot mirror — can await completion deterministically.
    youtube_boot_done = asyncio.Event()
    app.state.youtube_boot_done = youtube_boot_done
    transcript_boot_done = asyncio.Event()
    app.state.transcript_boot_done = transcript_boot_done

    # The likes sync only runs against a real upstream client; the in-memory fake
    # is a read-only seam with nothing to pull.
    if config.youtube_api is not None and config.youtube_sync_enabled:

        async def _run_likes_sync() -> None:
            # Non-eager boot pass: only syncs if the gate window has elapsed, so
            # repeated dev restarts don't each re-spend the day's budget. Boot
            # failures are logged, not fatal, and still release the barrier.
            try:
                _ = await sync.maybe_sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("YouTube likes boot sync failed")
            finally:
                youtube_boot_done.set()
            await sync.sync_forever(
                interval_seconds=config.youtube_sync_interval_seconds, logger=logger
            )

        tasks.append(asyncio.create_task(_run_likes_sync()))
    else:
        youtube_boot_done.set()

    # The transcript worker auto-runs only for an explicitly configured provider
    # (captions in production). The fake-derived provider still serves on-demand
    # fetches but does not start a background drain in tests.
    if config.transcript_provider is not None and config.transcript_sync_enabled:

        async def _run_transcript_sync() -> None:
            try:
                _ = await transcript_sync.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("YouTube transcript boot sync failed")
            finally:
                transcript_boot_done.set()
            await transcript_sync.sync_forever(
                interval_seconds=config.transcript_sync_interval_seconds,
                logger=logger,
            )

        tasks.append(asyncio.create_task(_run_transcript_sync()))
    else:
        transcript_boot_done.set()

    return tasks


def _build_scheduler(
    app: Starlette,
    *,
    config: AppConfig,
    database: Database,
    trigger_service: TriggerService,
    kb_root: Path,
) -> Scheduler:
    """Wire the Scheduled-trigger scheduler over its dispatch collaborators.

    Agent-prompt triggers spawn ephemeral pi processes under a dedicated session
    root; fixed-message triggers never touch pi. Delivery goes out over the
    in-process event hub as `notify` frames and is persisted through the
    notification service (registered here on `app.state`) so a fired reminder
    survives a reload. Shared collaborators (event hub, model catalog, session
    registry, tool secret, logger) are read from the already-populated
    `app.state`.
    """
    notification_service = NotificationService(
        database=database,
        event_publisher=cast("EventHub", app.state.event_hub),
    )
    app.state.notification_service = notification_service
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
            trace_recorder=cast("AgentTraceRecorder", app.state.trace_recorder),
            run_kind="scheduled",
        )
    )
    return Scheduler(
        service=trigger_service,
        dispatcher=TriggerDispatcher(
            notifier=EventNotifier(app.state.event_hub, notification_service),
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
                trace_recorder=cast("AgentTraceRecorder", app.state.trace_recorder),
                run_kind="recall",
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


async def _build_search(
    *,
    database: Database,
    embedder: Embedder | None,
    index_dir: Path,
    logger: Logger,
) -> tuple[MemorySearchService | None, SearchReconciler | None]:
    """Wire the search subsystem when an embedder is supplied, else disable it.

    Opens the index, converges it with SQLite once on boot (embedding any owed
    tethered Memory and dropping orphans — a no-op, and no model load, on an
    empty corpus), and returns the searcher for `MemoryService` alongside the
    reconciler the lifespan drives on a periodic pass. With no embedder returns
    `(None, None)`: the index is never opened and no model loads."""
    if embedder is None:
        return None, None
    search_index = await SearchIndex.open(
        index_dir=index_dir, vector_dim=embedder.vector_dim
    )
    reconciler = SearchReconciler(
        database=database,
        index=search_index,
        embedder=embedder,
        meta=SearchMetaService(database=database),
    )
    _ = await reconciler.reconcile(logger=logger)
    searcher = MemorySearchService(
        embedder=embedder, index=search_index, writer=reconciler
    )
    return searcher, reconciler


async def _build_transcript_search(
    *,
    database: Database,
    embedder: Embedder | None,
    index_dir: Path,
) -> tuple[TranscriptSearchService | None, TranscriptReconciler | None]:
    """Wire semantic transcript search when an embedder is supplied, else disable.

    Opens the transcript-chunk index and returns the searcher `YouTubeService`
    uses alongside the reconciler the lifespan drives on a periodic pass. Unlike
    the Memory index there is no boot reconcile — a cold pass re-embeds the whole
    transcript corpus and would block startup, so the periodic loop fills it. With
    no embedder returns `(None, None)`: the index is never opened, search falls
    back to the lexical `LIKE` path, and no model loads."""
    if embedder is None:
        return None, None
    index = await TranscriptIndex.open(
        index_dir=index_dir, vector_dim=embedder.vector_dim
    )
    reconciler = TranscriptReconciler(database=database, index=index, embedder=embedder)
    searcher = TranscriptSearchService(embedder=embedder, index=index)
    return searcher, reconciler


def _reconcile_loop_tasks(
    *,
    search_reconciler: SearchReconciler | None,
    transcript_reconciler: TranscriptReconciler | None,
    interval_seconds: float,
    logger: Logger,
) -> list[asyncio.Task[None]]:
    """Periodic reconcile loops for the wired search indexes.

    Each loop is the correctness backstop for its index — sweeping orphans and
    running `optimize()` while the host is up. The Memory loop complements its
    boot reconcile; the transcript loop has no boot pass, so it is what fills the
    transcript index shortly after startup. Either is absent when its index was
    not wired (no embedder)."""
    tasks: list[asyncio.Task[None]] = []
    if search_reconciler is not None:
        tasks.append(
            asyncio.create_task(
                search_reconciler.reconcile_forever(
                    interval_seconds=interval_seconds, logger=logger
                )
            )
        )
    if transcript_reconciler is not None:
        tasks.append(
            asyncio.create_task(
                transcript_reconciler.reconcile_forever(
                    interval_seconds=interval_seconds, logger=logger
                )
            )
        )
    return tasks


def _lifespan(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings,
    embedder: Embedder | None = None,
) -> Callable[[Starlette], AbstractAsyncContextManager[None, bool | None]]:
    """Create lifespan wiring for a configured SQLite DB and KB root.

    `embedder` defaults to the in-host `FastEmbedder` (loads the ONNX model on
    first boot); tests inject a `FakeEmbedder` to keep the search path in the
    gate without a model download."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """Build the Memory service for the app lifetime and close it after."""
        app_logger = configure_logging(config.logging_level, log_file=config.log_file)
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
            # Search is wired only when an embedder is supplied. Production
            # (`create_app_from_environment`) passes a `FastEmbedder`; tests that
            # exercise search pass a `FakeEmbedder`; everything else runs with
            # search disabled and never opens the index or loads a model.
            searcher, search_reconciler = await _build_search(
                database=db,
                embedder=embedder,
                index_dir=configured_kb_root / "index",
                logger=app_logger,
            )
            (
                transcript_searcher,
                transcript_reconciler,
            ) = await _build_transcript_search(
                database=db,
                embedder=embedder,
                index_dir=configured_kb_root / "transcript-index",
            )
            memory_service = MemoryService(
                database=db,
                event_publisher=event_hub,
                kb_service=kb_service,
                tracer=telemetry.tracer,
                searcher=searcher,
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
            youtube_tasks = await _wire_youtube(
                app,
                config=config,
                database=db,
                event_publisher=event_hub,
                transcript_search=transcript_searcher,
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
                database=db,
                trigger_service=trigger_service,
                kb_root=configured_kb_root,
            )
            app.state.scheduler = scheduler
            background_tasks = [
                asyncio.create_task(runtime_registry.reap_idle_forever()),
                asyncio.create_task(scheduler.run_forever()),
            ]
            # The periodic search-index reconcile loops (Memory + transcript), each
            # started only when its index was wired (an embedder was supplied).
            background_tasks.extend(
                _reconcile_loop_tasks(
                    search_reconciler=search_reconciler,
                    transcript_reconciler=transcript_reconciler,
                    interval_seconds=config.search_reconcile_seconds,
                    logger=app_logger,
                )
            )
            # The YouTube ingestion sync loop (empty unless a real client is
            # configured) joins the cancelled-on-shutdown background tasks.
            background_tasks.extend(youtube_tasks)
            try:
                yield
            finally:
                for task in background_tasks:
                    _ = task.cancel()
                for task in background_tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await scheduler.shutdown()
                await runtime_registry.shutdown_all()
                telemetry.shutdown()

    return lifespan


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
) -> Starlette:
    """Construct the Starlette application with Memory routes and lifespan wiring.

    The public REST routes are also handed to `openapi_routes` so `/openapi.json`
    and `/docs` describe exactly the API that is mounted. By default, both the
    SQLite database and markdown Knowledge base live under `.tether`. `embedder`
    defaults to the in-host `FastEmbedder`; tests pass a `FakeEmbedder` to drive
    the search path without downloading a model.
    """
    api_routes = public_api_routes()
    docs = openapi_routes(api_routes, title="Tether", version="0.1.0")
    configured_telemetry = telemetry_settings or TelemetrySettings()
    spa_mount = _spa_mount(config.web_dist) if config.web_dist is not None else None
    app = Starlette(
        routes=[
            *api_routes,
            *trace_routes(),
            *internal_tool_routes(),
            *internal_bucket_tool_routes(),
            *internal_triage_tool_routes(),
            *internal_youtube_tool_routes(),
            *internal_trigger_tool_routes(),
            *internal_recall_tool_routes(),
            *websocket_routes,
            *docs,
            # The SPA catch-all mounts at "/", so it must come last — every API,
            # WS, and docs route above is matched before requests fall through to
            # the static shell. Absent in dev/test (no build configured).
            *([spa_mount] if spa_mount is not None else []),
        ],
        lifespan=_lifespan(
            config=config,
            telemetry_settings=configured_telemetry,
            embedder=embedder,
        ),
    )
    app.state.app_password = config.app_password
    app.state.secure_cookies = config.secure_cookies
    app.state.session_registry = SessionRegistry()
    app.state.trace_recorder = AgentTraceRecorder()
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


def build_configured_transcript_provider(
    settings: HostSettings,
) -> TranscriptProvider | None:
    """Build the transcript provider when a YouTube token has been authorized.

    Provider precedence follows what actually transcribes the liked corpus. The
    OAuth captions Data API is *owner-only*: it 403s for nearly every third-party
    (liked) video, so on its own it yields almost nothing. Supadata is the only
    source that reliably transcribes them. So when Supadata is keyed + enabled it
    is the **primary**, with the captions API and the free (IP-block-prone)
    `youtube-transcript-api` library trailing as best-effort fallbacks that cost no
    Supadata budget. Without Supadata the captions API leads and the library fills
    gaps (the prior behaviour). With no token, returns `None` so the on-demand path
    falls back to a null provider and the background transcript worker stays off.

    The per-call Supadata use cap is late-bound at wire time (it needs the
    database), so it is not applied here.
    """
    if not settings.youtube_token_path.exists():
        return None
    captions = CaptionsTranscriptProvider.from_config(_youtube_oauth_config(settings))
    library = (
        YouTubeTranscriptApiProvider() if settings.transcript_library_enabled else None
    )
    return _compose_transcript_provider(
        captions=captions, library=library, supadata=_build_supadata_provider(settings)
    )


def _compose_transcript_provider(
    *,
    captions: TranscriptProvider,
    library: TranscriptProvider | None,
    supadata: TranscriptProvider | None,
) -> TranscriptProvider:
    """Order the configured sources by what actually transcribes the liked corpus.

    Supadata, when present, leads (it is the only source that reliably transcribes
    third-party videos); the owner-only captions API and the free library trail it
    as best-effort fallbacks. Without Supadata the captions API leads and the
    library fills gaps. A single bare source is returned uncomposed.
    """
    if supadata is not None:
        fallbacks: list[TranscriptProvider] = [captions]
        if library is not None:
            fallbacks.append(library)
        return FallbackTranscriptProvider(supadata, fallbacks=fallbacks)
    if library is not None:
        return FallbackTranscriptProvider(captions, fallbacks=[library])
    return captions


def _build_supadata_provider(
    settings: HostSettings,
) -> SupadataTranscriptProvider | None:
    """Build the paid Supadata provider only when its key *and* flag are both set.

    Either missing makes Supadata a true no-op (omitted from the chain), so the
    free providers behave exactly as before and no cost can be incurred by
    accident.
    """
    if not (settings.supadata_enabled and settings.supadata_api_key):
        return None
    config = SupadataConfig(
        base_url=settings.supadata_base_url,
        timeout=timedelta(seconds=settings.supadata_timeout_seconds),
        poll_interval=timedelta(seconds=settings.supadata_poll_interval_seconds),
        max_poll_attempts=settings.supadata_max_poll_attempts,
        mode=settings.supadata_mode,
    )
    transport = HttpSupadataTransport(settings.supadata_api_key, config=config)
    return SupadataTranscriptProvider(transport, config=config)


def _youtube_oauth_config(settings: HostSettings) -> OAuthConfig:
    """Build the shared OAuth config for the YouTube adapters."""
    return OAuthConfig(
        token_path=settings.youtube_token_path,
        client_secret_path=settings.youtube_client_secret_path,
        no_browser=settings.youtube_oauth_no_browser,
    )


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
            log_file=settings.log_file,
            model_allowlist=settings.model_allowlist,
            secure_cookies=settings.secure_cookies,
            session_secret=settings.session_secret,
            web_dist=settings.web_dist,
            youtube_api=build_configured_youtube_api(settings),
            youtube_sync_enabled=settings.youtube_sync_enabled,
            transcript_provider=build_configured_transcript_provider(settings),
            transcript_supadata_max_uses=settings.supadata_max_uses,
            transcript_sync_enabled=settings.transcript_sync_enabled,
        ),
        telemetry_settings=settings.telemetry,
        tool_secret=settings.tool_secret,
        embedder=FastEmbedder(),
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
        configured_settings.logging_level, log_file=configured_settings.log_file
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
