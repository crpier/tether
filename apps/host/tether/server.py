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
from opentelemetry.trace import Tracer
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

from tether.agent_trace import AgentTraceRecorder, RunKind
from tether.artifact_tools import internal_artifact_tool_routes
from tether.artifacts import ArtifactService, create_artifact_schema
from tether.auth import AppSessionMiddleware
from tether.bucket_item_index import BucketItemIndex
from tether.bucket_item_reconciler import BucketItemReconciler
from tether.bucket_items import (
    BucketItemService,
    create_bucket_item_schema,
)
from tether.bucket_tools import internal_bucket_tool_routes
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.chat_ws import websocket_routes
from tether.conversation_history_tools import (
    internal_conversation_history_tool_routes,
)
from tether.conversations import ConversationService, create_conversation_schema
from tether.embeddings import Embedder, FastEmbedder
from tether.events import EventHub
from tether.kosync import KosyncService, create_kosync_schema
from tether.kosync_routes import KosyncAuth, kosync_protocol_routes
from tether.kosync_tools import internal_kosync_tool_routes
from tether.logging import ContextLoggerMiddleware, Logger, configure_logging
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.notifications import NotificationService, create_notification_schema
from tether.openapi import openapi_routes
from tether.openapi_export import public_api_routes
from tether.panel_tools import internal_panel_tool_routes
from tether.panels import PanelService, create_panel_schema
from tether.push import PushService, create_push_schema
from tether.readwise import (
    HttpReadwiseTransport,
    ReadwiseClient,
    ReadwiseSyncService,
    create_readwise_schema,
)
from tether.recall import (
    AnswerGrader,
    PiAnswerGrader,
    PiStudyItemGenerator,
    RecallModelSteps,
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
from tether.search_fusion import SearchFusionService
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
from tether.transcript_provider_composition import (
    build_configured_transcript_provider,
    resolve_transcript_provider,
)
from tether.transcript_reconciler import TranscriptReconciler
from tether.transcript_search import TranscriptSearchService
from tether.transcript_supadata import SupadataMode
from tether.transcript_worker import TranscriptSyncService
from tether.triage import TriageService
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.triggers import TriggerService, create_trigger_schema
from tether.youtube import (
    DailyQuota,
    InMemoryYouTubeApi,
    TranscriptProvider,
    TranscriptSyncConfig,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeService,
    YouTubeSyncConfig,
    YouTubeSyncService,
    create_youtube_schema,
)
from tether.youtube_oauth import OAuthConfig, OAuthYouTubeApi
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
    kosync_enabled: bool = False
    kosync_username: str = ""
    kosync_userkey: str = ""
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
    youtube_likes_rewalk_interval_days: float = 30.0
    youtube_likes_drift_alarm_margin: int = 5
    youtube_api_gate_pause_base_seconds: float = 15 * 60
    youtube_api_gate_pause_cap_seconds: float = 6 * 60 * 60
    transcript_provider: TranscriptProvider | None = None
    transcript_supadata_max_uses: int = 3_000
    transcript_sync_enabled: bool = True
    transcript_sync_interval_seconds: float = 5 * 60
    transcript_sync_recent_window: int = 50
    transcript_retry_backoff_base_seconds: float = 10 * 60
    transcript_retry_backoff_cap_seconds: float = 6 * 60 * 60
    transcript_block_pause_base_seconds: float = 2 * 60 * 60
    transcript_block_pause_cap_seconds: float = 24 * 60 * 60
    readwise_api_key: str = ""
    readwise_sync_enabled: bool = False
    readwise_sync_interval_seconds: float = 60 * 60
    pi_idle_seconds: float = 30 * 60
    pi_session_root: str | Path | None = None
    scheduler_concurrency: int = 4
    scheduler_tick_seconds: float = 30.0
    search_reconcile_seconds: float = 5 * 60
    secure_cookies: bool = False
    study_item_generator: StudyItemGenerator | None = None
    answer_grader: AnswerGrader | None = None
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
    kosync_enabled: bool = False
    """Whether the host serves the KOReader kosync protocol under `/kosync`. Off
    by default and a no-op unless `kosync_username` and `kosync_userkey` are both
    set, so a default install leaves the whole prefix unmounted (404). Devices
    must set KOReader's document-matching method to *filename* (hash =
    `md5(basename)`); the binary default cannot be mapped back to a title."""
    kosync_username: str = ""
    """The single pre-provisioned kosync username a device authenticates as
    (`x-auth-user`). Empty keeps the gate off."""
    kosync_userkey: str = ""
    """The `md5(password)` string the device sends as `x-auth-key`, compared
    verbatim. KOReader hashes the password itself; Tether never sees the
    plaintext. Empty keeps the gate off."""
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
    youtube_likes_rewalk_interval_days: float = 30.0
    """How long a completed likes backfill stays settled before the walk restarts.
    Once history has been mirrored the sync only refreshes the hot (newest) pages;
    it re-walks history from the tail once the last completion is older than this,
    catching likes that predate the corpus. Set high to walk history rarely."""
    youtube_likes_drift_alarm_margin: int = 5
    """How far the upstream liked-playlist total may exceed the local corpus before
    a settled backfill is treated as drifted and restarted immediately. Videos
    skipped locally (deleted, private, members-only) are tracked by id and folded
    into the comparison precisely, so this margin only absorbs transient races."""
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
    """Whether the `youtube-transcript-api` library source is available to compose.
    Enabled by default; set `TETHER_TRANSCRIPT_LIBRARY_ENABLED=false` to drop it
    from the chain entirely (e.g. if the host IP keeps getting blocked)."""
    transcript_library_max_requests_per_pass: int = 5
    """Hard cap on real `youtube-transcript-api` network calls within a single
    transcript sync pass. Deliberately small and strict: the library gets the host
    IP-blocked in bursts of roughly 10+ rapid requests (issue #179), so one pass
    must never be allowed to fire dozens at it. Once the cap is spent the provider
    self-throttles for the rest of that pass (remaining candidates stay pending,
    picked up next pass) rather than making further real calls; a fresh pass gets
    a fresh budget. Applies to `youtube_transcript_api` only — Supadata's own
    budget (`supadata_max_uses`) and pacing (`supadata_min_request_interval_seconds`)
    are unaffected."""
    transcript_library_min_request_interval_seconds: float = 5.0
    """Minimum spacing between consecutive real `youtube-transcript-api` calls.
    Mirrors `supadata_min_request_interval_seconds`: back-to-back requests read as
    bot traffic to YouTube, so pacing even the small per-pass budget's calls keeps
    the host looking less like a scraper. 0 disables pacing."""
    transcript_block_pause_base_seconds: float = 2 * 60 * 60
    """Initial cooldown once a blockable transcript source (the free library, or
    Supadata) reports an IP block / rate limit, before its escalating per-source
    pause is retried. Raised from a historical 30 minutes: youtube-transcript-api's
    IP blocks routinely outlast a half hour, so a short initial cooldown just
    re-triggers the same block on the very next pass. Doubles on each further
    consecutive block, clamped to `transcript_block_pause_cap_seconds`."""
    transcript_block_pause_cap_seconds: float = 24 * 60 * 60
    """Ceiling on the escalating per-source transcript-provider pause. Raised from
    6 hours to a full day: a source still getting blocked after several
    escalations is very likely under a longer-lived IP ban, so backing off for up
    to a day is worth the lost sync speed (explicitly an acceptable trade per
    issue #179)."""
    transcript_languages: str = "en,ro"
    """Comma-separated preferred transcript languages, most preferred first (ISO
    codes). Passed to the `youtube-transcript-api` library (which tries them in
    order) and to Supadata (which requests the most preferred track), replacing the
    old hardcoded English-only preference. The default is English primary, Romanian
    secondary."""
    transcript_provider_order: str = "supadata,library"
    """Comma-separated transcript sources, primary first, that compose the fetch
    chain. Known names: `supadata` (paid, the reliable primary for third-party
    videos), `library` (the free `youtube-transcript-api`), `captions` (the
    owner-only Data API, dropped from the default order because it transcribes
    almost none of the liked corpus). A named source that is unconfigured (Supadata
    without a key, the library disabled) is skipped; an *unknown* name is rejected
    at startup. The default leads with Supadata and trails with the free library."""
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
    supadata_min_request_interval_seconds: float = 1.0
    """Minimum spacing between billed Supadata submits. The transcript sweep fetches
    videos back-to-back, so a low-rate plan returns `429 limit-exceeded` on the burst
    and pauses the source; spacing submits keeps them under that per-request rate. The
    1.0s default suits a modest plan; set 0 to disable pacing on a generous one."""
    supadata_mode: SupadataMode = "native"
    """Supadata transcript mode. `native` (the default) fetches an existing caption
    track only — one use per call — so a caption-less video costs one lookup and
    returns unavailable instead of the multi-use AI `generate` path."""
    supadata_max_uses: int = 3_000
    """Hard cap on total Supadata uses, persisted across restarts. The background
    sweep stops calling Supadata once this many are spent (remaining videos stay
    pending), bounding spend to a limited plan. Raise it after topping up."""
    readwise_api_key: str = ""
    """Readwise API token. Empty (the default) keeps the ingestion gate off, so
    the default install never calls Readwise. Paired with
    `readwise_sync_enabled`; both are required for the worker to run."""
    readwise_sync_enabled: bool = False
    """Whether the background Readwise ingestion gate runs. Off by default and a
    no-op unless `readwise_api_key` is also set, so mirroring highlights into the
    Commons is a deliberate, credentialed choice."""
    readwise_sync_interval_seconds: float = 60 * 60
    """Seconds between Readwise export passes. The Export API is generous (240
    req/min) but highlights change slowly, so an hourly cadence is ample."""
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
    await create_artifact_schema(db)
    await create_panel_schema(db)
    await create_readwise_schema(db)
    await create_kosync_schema(db)


def _build_youtube_client(
    api: YouTubeApi, config: AppConfig, database: Database
) -> YouTubeApiClient:
    """Wrap the upstream API in the budgeted, gated client the workers share."""
    return YouTubeApiClient(
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


def _build_transcript_sync_config(config: AppConfig) -> TranscriptSyncConfig:
    """The shared transcript retry/backoff config for the worker and on-demand path."""
    return TranscriptSyncConfig(
        recent_window=config.transcript_sync_recent_window,
        backoff_base=timedelta(seconds=config.transcript_retry_backoff_base_seconds),
        backoff_cap=timedelta(seconds=config.transcript_retry_backoff_cap_seconds),
        block_pause_base=timedelta(seconds=config.transcript_block_pause_base_seconds),
        block_pause_cap=timedelta(seconds=config.transcript_block_pause_cap_seconds),
    )


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
    client = _build_youtube_client(api, config, database)
    provider = resolve_transcript_provider(
        configured_provider=config.transcript_provider,
        api=api,
        database=database,
        client=client,
        supadata_max_uses=config.transcript_supadata_max_uses,
    )
    transcript_config = _build_transcript_sync_config(config)
    youtube_service = YouTubeService(
        database=database,
        client=client,
        provider=provider,
        event_publisher=event_publisher,
        tracer=tracer,
    )
    # Late-bind the on-demand retry config to the same one the worker uses, and
    # the optional semantic-search collaborator (None when search is disabled,
    # leaving the lexical LIKE fallback in place). Per-source usage (e.g.
    # Supadata's monthly spend) is read straight off `provider` by the status
    # surface — no separate late-bound reader needed.
    youtube_service.config = transcript_config
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
            rewalk_interval=timedelta(days=config.youtube_likes_rewalk_interval_days),
            drift_alarm_margin=config.youtube_likes_drift_alarm_margin,
        ),
        event_publisher=event_publisher,
    )
    app.state.youtube_sync = sync
    transcript_sync = TranscriptSyncService(
        database=database,
        client=client,
        provider=provider,
        config=transcript_config,
        event_publisher=event_publisher,
    )
    app.state.transcript_sync = transcript_sync
    return _start_youtube_workers(
        app, config=config, logger=logger, sync=sync, transcript_sync=transcript_sync
    )


def _start_youtube_workers(
    app: Starlette,
    *,
    config: AppConfig,
    logger: Logger,
    sync: YouTubeSyncService,
    transcript_sync: TranscriptSyncService,
) -> list[asyncio.Task[None]]:
    """Launch the likes + transcript boot passes and periodic loops off the critical
    path, and return the loop tasks.

    Boot passes run off the startup critical path so the lifespan completes and
    uvicorn binds its port immediately; a slow first sync no longer hangs startup.
    Each worker's `<...>_boot_done` barrier is set once its boot pass finishes (or is
    skipped), so a readiness probe and boot-mirror tests can await it. Each worker
    starts only when its real upstream is configured (a likes client / a transcript
    provider); otherwise its barrier is released immediately and no loop runs.
    """
    tasks: list[asyncio.Task[None]] = []
    youtube_boot_done = asyncio.Event()
    app.state.youtube_boot_done = youtube_boot_done
    transcript_boot_done = asyncio.Event()
    app.state.transcript_boot_done = transcript_boot_done

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


async def _wire_readwise(
    app: Starlette,
    *,
    config: AppConfig,
    database: Database,
    memory_service: MemoryService,
) -> list[asyncio.Task[None]]:
    """Wire the Readwise ingestion gate + its background worker onto state.

    A no-op returning no tasks unless the gate is enabled and an API key is set —
    the default install never calls Readwise. When enabled, the token is checked
    off the startup critical path inside the worker task (a non-204 auth response
    logs a warning and disables the worker for the run); a valid token runs an
    idempotent boot pass and then the periodic export loop. The returned tasks
    join the lifespan's cancelled-on-shutdown background tasks.
    """
    readwise_boot_done = asyncio.Event()
    app.state.readwise_boot_done = readwise_boot_done
    if not config.readwise_sync_enabled or not config.readwise_api_key:
        readwise_boot_done.set()
        return []
    logger = cast("Logger", app.state.logger)
    client = ReadwiseClient(transport=HttpReadwiseTransport(config.readwise_api_key))
    sync = ReadwiseSyncService(
        database=database, client=client, memory_service=memory_service
    )
    app.state.readwise_sync = sync

    async def _run_readwise_sync() -> None:
        try:
            if not await client.verify_token(logger=logger):
                logger.warning("Readwise token invalid; ingestion gate disabled")
                return
            _ = await sync.sync(logger=logger)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Readwise boot sync failed")
        finally:
            readwise_boot_done.set()
        await sync.sync_forever(
            interval_seconds=config.readwise_sync_interval_seconds, logger=logger
        )

    return [asyncio.create_task(_run_readwise_sync())]


def _ephemeral_pi_config(
    app: Starlette,
    *,
    config: AppConfig,
    kb_root: Path,
    run_kind: RunKind,
    model: AgentModelConfig | None,
) -> EphemeralPiConfig:
    """Build the wiring shared by every ephemeral pi runner (scheduled, recall).

    The two run kinds differ only in their session subdir (named after
    `run_kind` itself) and the `run_kind` carried on the config (which selects
    the system prompt and trace-run label); everything else (session registry,
    tool credentials, extension paths, trace recorder) comes straight from
    `app.state`/`config`, so a future shared field (like `trace_recorder`) only
    needs to be added here once.
    """
    session_root = (
        Path(config.pi_session_root)
        if config.pi_session_root is not None
        else kb_root / "pi-sessions"
    )
    return EphemeralPiConfig(
        session_registry=app.state.session_registry,
        session_root=session_root / run_kind,
        tool_base_url=config.tool_base_url,
        tool_secret=app.state.tool_secret,
        model=model,
        extra_extension_paths=config.extra_extension_paths,
        pi_binary=config.pi_binary,
        trace_recorder=cast("AgentTraceRecorder", app.state.trace_recorder),
        run_kind=run_kind,
    )


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
    prompt_runner = EphemeralPiPromptRunner(
        _ephemeral_pi_config(
            app,
            config=config,
            kb_root=kb_root,
            run_kind="scheduled",
            model=model_catalog.default_config,
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
    """Wire the Recall service over its model-backed generator and grader.

    Distilling a transcript into learnings + prompts and judging free-text
    answers are the model steps in Recall, so both run an ephemeral pi under a
    dedicated session root (one shared runner); everything else (deterministic
    grading, scheduling, the completion tether) is pure. Shared collaborators
    are read from the already-populated `app.state`.
    """
    generator: StudyItemGenerator | None = config.study_item_generator
    grader: AnswerGrader | None = config.answer_grader
    if generator is None or grader is None:
        model_catalog = cast("AgentModelCatalog", app.state.model_catalog)
        runner = EphemeralPiPromptRunner(
            _ephemeral_pi_config(
                app,
                config=config,
                kb_root=kb_root,
                run_kind="recall",
                model=model_catalog.default_config,
            )
        )
        generator = generator or PiStudyItemGenerator(runner)
        grader = grader or PiAnswerGrader(runner)
    telemetry = cast("Telemetry", app.state.telemetry)
    return RecallService(
        database=database,
        memory_service=memory_service,
        models=RecallModelSteps(generator=generator, grader=grader),
        event_publisher=cast("EventHub", app.state.event_hub),
        tracer=telemetry.tracer,
    )


async def _build_search(
    *,
    database: Database,
    embedder: Embedder | None,
    index_dir: Path,
    logger: Logger,
) -> SearchReconciler | None:
    """Wire the search subsystem when an embedder is supplied, else disable it.

    Opens the index, converges it with SQLite once on boot (embedding any owed
    tethered Memory and dropping orphans — a no-op, and no model load, on an
    empty corpus), and returns the reconciler: the single search seam that both
    reads for `MemoryService` and is driven by the lifespan's periodic pass. With
    no embedder returns `None`: the index is never opened and no model loads."""
    if embedder is None:
        return None
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
    return reconciler


async def _build_bucket_item_search(
    *,
    database: Database,
    embedder: Embedder | None,
    index_dir: Path,
    logger: Logger,
) -> BucketItemReconciler | None:
    """Wire the Bucket-item search subsystem when an embedder is supplied.

    Mirrors `_build_search`: opens the index, converges it with SQLite once on
    boot (embedding any owed active Bucket item and dropping orphans — a no-op
    on an empty corpus), and returns the reconciler: the single search seam
    `BucketItemService` reads through and the lifespan drives on a periodic
    pass. With no embedder returns `None`: the index is never opened and no
    model loads, and Bucket-item search stays unavailable (same as Memory
    search)."""
    if embedder is None:
        return None
    bucket_item_index = await BucketItemIndex.open(
        index_dir=index_dir, vector_dim=embedder.vector_dim
    )
    reconciler = BucketItemReconciler(
        database=database,
        index=bucket_item_index,
        embedder=embedder,
        meta=SearchMetaService(database=database),
    )
    _ = await reconciler.reconcile(logger=logger)
    return reconciler


def _build_bucket_item_and_fusion_services(
    *,
    database: Database,
    event_hub: EventHub,
    memory_service: MemoryService,
    searcher: BucketItemReconciler | None,
    tracer: Tracer,
) -> tuple[BucketItemService, SearchFusionService]:
    """Wire the Bucket-item service and the cross-source fusion service above it.

    Fusion depends on both the Bucket-item and Memory services existing, so
    building them together keeps that dependency explicit at the one call site
    instead of splitting it across two `_lifespan` statements."""
    bucket_item_service = BucketItemService(
        database=database,
        event_publisher=event_hub,
        tracer=tracer,
        searcher=searcher,
    )
    search_fusion_service = SearchFusionService(
        bucket_item_service=bucket_item_service, memory_service=memory_service
    )
    return bucket_item_service, search_fusion_service


def _build_presentation_services(
    app: Starlette,
    *,
    config: AppConfig,
    database: Database,
    event_hub: EventHub,
    tracer: Tracer,
) -> None:
    """Wire the presentation-side services plus the kosync gate's app-state.

    Reads the Memory service back off app state (already wired by the lifespan)
    rather than taking it as a parameter — panels execute through the Memory
    search seam and the kosync gate captures finished books through it. The
    `KosyncService` is wired here unconditionally so the owner-facing labeling
    REST/tools work even with the device protocol disabled; its device
    `x-auth-user`/`x-auth-key` credentials are only read by the `/kosync/*`
    routes, which `create_app` mounts only when the gate is configured, so blank
    credentials are harmless when disabled. All are pure app-state singletons
    with no background tasks."""
    memory_service = cast("MemoryService", app.state.memory_service)
    app.state.artifact_service = ArtifactService(
        database=database,
        event_publisher=event_hub,
        tracer=tracer,
    )
    app.state.panel_service = PanelService(
        database=database,
        memory_service=memory_service,
        event_publisher=event_hub,
        tracer=tracer,
    )
    app.state.kosync_service = KosyncService(
        database=database, memory_service=memory_service
    )
    app.state.kosync_auth = KosyncAuth(
        username=config.kosync_username, userkey=config.kosync_userkey
    )


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
    bucket_item_reconciler: BucketItemReconciler | None,
    transcript_reconciler: TranscriptReconciler | None,
    interval_seconds: float,
    logger: Logger,
) -> list[asyncio.Task[None]]:
    """Periodic reconcile loops for the wired search indexes.

    Each loop is the correctness backstop for its index — sweeping orphans and
    running `optimize()` while the host is up. The Memory and Bucket-item loops
    complement their own boot reconcile; the transcript loop has no boot pass,
    so it is what fills the transcript index shortly after startup. Any of the
    three is absent when its index was not wired (no embedder)."""
    tasks: list[asyncio.Task[None]] = []
    if search_reconciler is not None:
        tasks.append(
            asyncio.create_task(
                search_reconciler.reconcile_forever(
                    interval_seconds=interval_seconds, logger=logger
                )
            )
        )
    if bucket_item_reconciler is not None:
        tasks.append(
            asyncio.create_task(
                bucket_item_reconciler.reconcile_forever(
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


_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS = 5.0
"""Bound on how long lifespan shutdown waits for background tasks to unwind.

Previously the finally block awaited every background task with no bound. A
task that doesn't propagate `CancelledError` back out promptly — including,
in practice, the YouTube/transcript sync loops while inside a synchronous
`asyncio.to_thread` upstream call, which the cancelling task can't interrupt
mid-call — could hold shutdown open for however long that happened to take
(observed: up to ~2 minutes under `just dev`, which the reload supervisor's
`process.join()` then waits on in turn, leaving the whole process tree
running well after ctrl-c). Past this grace period we log and abandon
whatever hasn't finished instead of blocking on it further. Note this bounds
our own `await`, not the underlying OS thread a `to_thread` call may still be
running in the background — `just dev`'s cleanup trap force-kills the
process group as the outer backstop for that.
"""


async def _shutdown_background_tasks(
    tasks: Sequence[asyncio.Task[None]],
    *,
    logger: Logger,
    grace_seconds: float = _BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS,
) -> None:
    """Cancel `tasks` and await them without blocking shutdown indefinitely.

    Tasks that finish (by honoring cancellation) within `grace_seconds` are
    awaited normally. Anything still outstanding past the grace period is
    logged and left to run to completion in the background — the process is
    exiting either way, so nothing further awaits it.
    """
    for task in tasks:
        _ = task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
    for task in pending:
        logger.warning(
            "Background task did not stop within the shutdown grace period; abandoning it",
            task=task.get_name(),
        )
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            await task


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
            search_reconciler = await _build_search(
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
                searcher=search_reconciler,
            )
            await memory_service.regenerate_knowledge_base(logger=app_logger)
            app.state.memory_service = memory_service
            # The digest reuses the same embedder as search: semantic dedup and
            # contradiction recall when it is wired, keyword fallback when not.
            app.state.review_service = ReviewService(database=db, embedder=embedder)
            app.state.triage_service = TriageService(database=db)
            bucket_item_reconciler = await _build_bucket_item_search(
                database=db,
                embedder=embedder,
                index_dir=configured_kb_root / "bucket-item-index",
                logger=app_logger,
            )
            (
                app.state.bucket_item_service,
                app.state.search_fusion_service,
            ) = _build_bucket_item_and_fusion_services(
                database=db,
                event_hub=event_hub,
                memory_service=memory_service,
                searcher=bucket_item_reconciler,
                tracer=telemetry.tracer,
            )
            _build_presentation_services(
                app,
                config=config,
                database=db,
                event_hub=event_hub,
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
            # The periodic search-index reconcile loops (Memory + Bucket-item +
            # transcript), each started only when its index was wired (an
            # embedder was supplied).
            background_tasks.extend(
                _reconcile_loop_tasks(
                    search_reconciler=search_reconciler,
                    bucket_item_reconciler=bucket_item_reconciler,
                    transcript_reconciler=transcript_reconciler,
                    interval_seconds=config.search_reconcile_seconds,
                    logger=app_logger,
                )
            )
            # The YouTube ingestion sync loop and the Readwise ingestion gate
            # (each empty unless its upstream is configured) join the
            # cancelled-on-shutdown background tasks.
            background_tasks.extend(
                youtube_tasks
                + await _wire_readwise(
                    app, config=config, database=db, memory_service=memory_service
                )
            )
            try:
                yield
            finally:
                await _shutdown_background_tasks(background_tasks, logger=app_logger)
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
            *internal_artifact_tool_routes(),
            *internal_triage_tool_routes(),
            *internal_youtube_tool_routes(),
            *internal_trigger_tool_routes(),
            *internal_recall_tool_routes(),
            *internal_conversation_history_tool_routes(),
            *internal_panel_tool_routes(),
            *internal_kosync_tool_routes(),
            # The device-facing kosync protocol is mounted only when configured
            # (username + userkey set): a disabled install leaves `/kosync/*`
            # unhandled, so it answers 404 rather than a live-but-empty gate.
            *(
                kosync_protocol_routes()
                if config.kosync_enabled
                and config.kosync_username
                and config.kosync_userkey
                else []
            ),
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


def _youtube_oauth_config(settings: HostSettings) -> OAuthConfig:
    """Build the shared OAuth config for the YouTube adapters."""
    return OAuthConfig(
        token_path=settings.youtube_token_path,
        client_secret_path=settings.youtube_client_secret_path,
        no_browser=settings.youtube_oauth_no_browser,
    )


def _app_config_from_settings(settings: HostSettings) -> AppConfig:
    """Build the `AppConfig` the app factory wires from environment settings.

    Extracted out of `create_app_from_environment` so the settings -> config
    field mapping is unit-testable without spinning up the full ASGI app (which
    needs a YouTube OAuth token on disk to wire the background workers).
    """
    return AppConfig(
        app_password=settings.app_password,
        database_path=settings.database_path,
        default_model=settings.default_model,
        kb_root=settings.kb_root,
        kosync_enabled=settings.kosync_enabled,
        kosync_username=settings.kosync_username,
        kosync_userkey=settings.kosync_userkey,
        logging_level=settings.logging_level,
        log_file=settings.log_file,
        model_allowlist=settings.model_allowlist,
        readwise_api_key=settings.readwise_api_key,
        readwise_sync_enabled=settings.readwise_sync_enabled,
        readwise_sync_interval_seconds=settings.readwise_sync_interval_seconds,
        secure_cookies=settings.secure_cookies,
        session_secret=settings.session_secret,
        web_dist=settings.web_dist,
        youtube_api=build_configured_youtube_api(settings),
        youtube_likes_rewalk_interval_days=settings.youtube_likes_rewalk_interval_days,
        youtube_likes_drift_alarm_margin=settings.youtube_likes_drift_alarm_margin,
        youtube_sync_enabled=settings.youtube_sync_enabled,
        transcript_provider=build_configured_transcript_provider(settings),
        transcript_supadata_max_uses=settings.supadata_max_uses,
        transcript_sync_enabled=settings.transcript_sync_enabled,
        # The escalating per-source pause bounds (issue #179): previously left at
        # AppConfig's own hardcoded defaults with no env-var override at all, so
        # threaded through here alongside the new library-specific knobs above.
        transcript_block_pause_base_seconds=settings.transcript_block_pause_base_seconds,
        transcript_block_pause_cap_seconds=settings.transcript_block_pause_cap_seconds,
    )


def create_app_from_environment() -> Starlette:
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
