"""Typed host composition and application-lifetime ownership."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anyio import Path as AsyncPath
from fastapi import FastAPI
from opentelemetry.trace import Tracer
from snekok import Err
from snekql.sqlite import Config, Database

from tether.action_registry import (
    ActionContext,
    all_action_specs,
    build_action_registry,
)
from tether.agent_trace import AgentTraceRecorder, RunKind
from tether.app_runtime import AppRuntime, install_app_runtime
from tether.artifacts import ArtifactService, create_artifact_schema
from tether.bucket_item_index import BucketItemIndex
from tether.bucket_item_reconciler import BucketItemReconciler
from tether.bucket_items import (
    BucketItemService,
    create_bucket_item_schema,
)
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.conversations import ConversationService, create_conversation_schema
from tether.ebook_stats import EbookStatsSyncService, create_ebook_stats_schema
from tether.embeddings import Embedder
from tether.events import EventHub
from tether.gmail import (
    GmailClient,
    GmailSyncService,
    create_gmail_schema,
)
from tether.gmail_purge import GmailPurgeSweepService
from tether.health_connect import (
    HealthConnectIngestion,
    create_health_connect_schema,
)
from tether.health_connect_telemetry import HealthConnectTelemetry
from tether.host_config import AppConfig
from tether.ingestion_lifecycle import (
    CallbackIngestionWorker,
    IngestionBootOutcome,
    IngestionLifecycle,
)
from tether.kosync import KosyncService, create_kosync_schema
from tether.kosync_routes import KosyncAuth
from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.notifications import NotificationService, create_notification_schema
from tether.panels import PanelService, create_panel_schema
from tether.proposals import ProposalService, create_proposal_schema
from tether.provider_auth import (
    ProviderAuthService,
    SubprocessProviderAuthBackend,
    provider_auth_helper_command,
)
from tether.push import (
    PushService,
    StoredPushSender,
    VapidConfig,
    VapidWebPushTransport,
    create_push_schema,
)
from tether.reader import ReaderClient, ReaderSyncService
from tether.readwise import ReadwiseClient, ReadwiseSyncService
from tether.readwise_http import (
    HttpReaderTransport,
    HttpReadwiseTransport,
    ReadwiseAuthenticationFailure,
)
from tether.readwise_store import create_readwise_schema
from tether.recall import RecallModelSteps, RecallService
from tether.recall_generation import PiStudyItemGenerator, StudyItemGenerator
from tether.recall_grading import AnswerGrader, PiAnswerGrader
from tether.recall_store import create_recall_schema
from tether.reconciler import SearchReconciler
from tether.review import ReviewService
from tether.scheduler import (
    EphemeralPiConfig,
    EphemeralPiPromptRunner,
    EventNotifier,
    PushDeliveryNotifier,
    Scheduler,
    SchedulerConfig,
    SystemClock,
    TriggerDispatcher,
    TriggerNotifier,
)
from tether.search_fusion import SearchFusionService
from tether.search_index import SearchIndex
from tether.search_meta import SearchMetaService, create_search_meta_schema
from tether.search_tools import (
    PersistentSearchSpendGuard,
    SearchProvider,
    TavilySearchProvider,
)
from tether.structured_logging import (
    QUIET_LOGGERS,
    Logger,
    configure_logging,
)
from tether.stt import SttClient
from tether.telemetry import (
    Telemetry,
    TelemetrySettings,
    configure_telemetry,
)
from tether.todo_digest import render_todo_digest
from tether.todos import (
    TodoService,
    create_todo_schema,
    migrate_pending_action_facets,
)
from tether.tools import SessionRegistry
from tether.transcripts.acquisition import (
    TranscriptAcquisitionService,
)
from tether.transcripts.contracts import (
    AsyncClosable,
    TranscriptProviderChain,
)
from tether.transcripts.worker import TranscriptSyncService
from tether.triage import TriageService
from tether.triggers import TriggerService, create_trigger_schema
from tether.youtube import YouTubeService
from tether.youtube_local import InMemoryYouTubeApi
from tether.youtube_quota import (
    DailyQuota,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
)
from tether.youtube_quota import (
    SystemClock as YouTubeSystemClock,
)
from tether.youtube_search import YouTubeSearchService
from tether.youtube_search_index import YouTubeSearchIndex
from tether.youtube_search_reconciler import YouTubeSearchReconciler
from tether.youtube_store import create_youtube_schema
from tether.youtube_sync import YouTubeSyncConfig, YouTubeSyncService


@dataclass(frozen=True, slots=True)
class HostBootstrap:
    """Process-local dependencies created before application startup."""

    session_registry: SessionRegistry
    stt_client: SttClient
    tool_secret: str
    trace_recorder: AgentTraceRecorder


@dataclass(frozen=True, slots=True)
class YouTubeComponent:
    """YouTube request service and observable worker readiness."""

    likes_ready: asyncio.Event
    service: YouTubeService
    transcripts_ready: asyncio.Event


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
    await create_proposal_schema(db)
    await create_artifact_schema(db)
    await create_panel_schema(db)
    await create_todo_schema(db)
    await create_readwise_schema(db)
    await create_kosync_schema(db)
    await create_ebook_stats_schema(db)
    await create_gmail_schema(db)


def _build_youtube_client(
    api: YouTubeApi, config: AppConfig, database: Database
) -> YouTubeApiClient:
    """Wrap the upstream API in the budgeted, gated client the workers share."""
    return YouTubeApiClient(
        api,
        DailyQuota(database, limit=config.youtube_daily_quota_limit),
        clock=YouTubeSystemClock(),
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


async def _wire_youtube(  # noqa: PLR0913 - composition requires each dependency
    *,
    config: AppConfig,
    database: Database,
    event_publisher: EventHub,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
    tracer: Tracer,
    youtube_search: YouTubeSearchService | None = None,
) -> YouTubeComponent:
    """Compose YouTube requests and workers from explicit host dependencies."""
    api = config.youtube_api or InMemoryYouTubeApi()
    client = _build_youtube_client(api, config, database)
    configured_provider = config.transcript_provider or (
        api if isinstance(api, InMemoryYouTubeApi) else None
    )
    provider = (
        configured_provider
        if isinstance(configured_provider, TranscriptProviderChain)
        else TranscriptProviderChain([configured_provider])
        if configured_provider is not None
        else None
    )
    acquisition = (
        TranscriptAcquisitionService(
            database=database,
            provider=provider,
            config=config.transcript_acquisition_config,
            event_publisher=event_publisher,
        )
        if provider is not None
        else None
    )
    youtube_service = YouTubeService(
        acquisition=acquisition,
        database=database,
        client=client,
        event_publisher=event_publisher,
        tracer=tracer,
        youtube_search=youtube_search,
    )
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
    transcript_sync = (
        TranscriptSyncService(
            acquisition=acquisition,
            clock=client,
            database=database,
            config=config.transcript_sync_config,
        )
        if acquisition is not None
        else None
    )
    likes_ready, transcripts_ready = _activate_youtube_workers(
        config=config,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
        sync=sync,
        transcript_sync=transcript_sync,
    )
    return YouTubeComponent(
        likes_ready=likes_ready,
        service=youtube_service,
        transcripts_ready=transcripts_ready,
    )


def _activate_youtube_workers(
    *,
    config: AppConfig,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
    sync: YouTubeSyncService,
    transcript_sync: TranscriptSyncService | None,
) -> tuple[asyncio.Event, asyncio.Event]:
    """Adapt YouTube's two source policies to the shared lifecycle owner."""
    likes_worker: CallbackIngestionWorker | None = None
    if config.youtube_api is not None and config.youtube_sync_enabled:

        async def _boot_likes() -> IngestionBootOutcome:
            _ = await sync.maybe_sync(logger=logger)
            return IngestionBootOutcome.REPEAT

        async def _repeat_likes() -> None:
            await sync.sync_forever(
                interval_seconds=config.youtube_sync_interval_seconds, logger=logger
            )

        likes_worker = CallbackIngestionWorker(_boot_likes, _repeat_likes)
    youtube_boot_done = ingestion_lifecycle.activate("youtube-likes", likes_worker)

    transcript_worker: CallbackIngestionWorker | None = None
    if (
        transcript_sync is not None
        and config.youtube_api is not None
        and config.transcript_provider is not None
        and config.transcript_sync_enabled
    ):

        async def _boot_transcripts() -> IngestionBootOutcome:
            _ = await transcript_sync.sync(logger=logger)
            return IngestionBootOutcome.REPEAT

        async def _repeat_transcripts() -> None:
            await transcript_sync.sync_forever(
                interval_seconds=config.transcript_sync_interval_seconds,
                logger=logger,
            )

        transcript_worker = CallbackIngestionWorker(
            _boot_transcripts, _repeat_transcripts
        )
    return youtube_boot_done, ingestion_lifecycle.activate(
        "youtube-transcripts", transcript_worker
    )


async def _wire_readwise(  # noqa: PLR0913 - composition owns every dependency
    *,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
    memory_service: MemoryService,
    resources: contextlib.AsyncExitStack,
) -> None:
    """Compose the optional Readwise export adapter into Ingestion lifecycle."""
    if not config.readwise_sync_enabled or (
        config.readwise_transport is None and not config.readwise_api_key
    ):
        _ = ingestion_lifecycle.activate("readwise")
        return
    transport = config.readwise_transport or HttpReadwiseTransport(
        config.readwise_api_key
    )
    _ = resources.push_async_callback(transport.aclose)
    client = ReadwiseClient(transport=transport)
    sync = ReadwiseSyncService(
        database=database, client=client, memory_service=memory_service
    )

    async def _boot_readwise() -> IngestionBootOutcome:
        token = await client.verify_token(logger=logger)
        if isinstance(token, Err):
            logger.warning(
                "Readwise token check failed",
                failure=type(token.error).__name__,
                operation=token.error.operation,
            )
            return (
                IngestionBootOutcome.STOP
                if isinstance(token.error, ReadwiseAuthenticationFailure)
                else IngestionBootOutcome.REPEAT
            )
        report = await sync.sync(logger=logger)
        if isinstance(report, Err):
            logger.warning(
                "Readwise boot sync failed",
                failure=type(report.error).__name__,
                operation=report.error.operation,
            )
        return IngestionBootOutcome.REPEAT

    async def _repeat_readwise() -> None:
        await sync.sync_forever(
            interval_seconds=config.readwise_sync_interval_seconds, logger=logger
        )

    _ = ingestion_lifecycle.activate(
        "readwise", CallbackIngestionWorker(_boot_readwise, _repeat_readwise)
    )


async def _wire_reader(  # noqa: PLR0913 - composition owns every dependency
    *,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
    memory_service: MemoryService,
    resources: contextlib.AsyncExitStack,
) -> None:
    """Compose the optional Reader progress adapter into Ingestion lifecycle."""
    if not config.readwise_reader_sync_enabled or (
        config.reader_transport is None and not config.readwise_api_key
    ):
        _ = ingestion_lifecycle.activate("readwise-reader")
        return
    transport = config.reader_transport or HttpReaderTransport(config.readwise_api_key)
    _ = resources.push_async_callback(transport.aclose)
    sync = ReaderSyncService(
        database=database,
        client=ReaderClient(transport=transport),
        memory_service=memory_service,
    )

    async def _boot_reader() -> IngestionBootOutcome:
        report = await sync.sync(logger=logger)
        if isinstance(report, Err):
            logger.warning(
                "Reader boot sync failed",
                failure=type(report.error).__name__,
                operation=report.error.operation,
            )
            if isinstance(report.error, ReadwiseAuthenticationFailure):
                return IngestionBootOutcome.STOP
        return IngestionBootOutcome.REPEAT

    async def _repeat_reader() -> None:
        await sync.sync_forever(
            interval_seconds=config.readwise_reader_sync_interval_seconds,
            logger=logger,
        )

    _ = ingestion_lifecycle.activate(
        "readwise-reader", CallbackIngestionWorker(_boot_reader, _repeat_reader)
    )


async def _wire_gmail(  # noqa: PLR0913 - each param is an independent wiring dependency
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    kb_root: Path,
    logger: Logger,
    memory_service: MemoryService,
    model_catalog: AgentModelCatalog,
    trigger_service: TriggerService,
    todo_service: TodoService,
) -> None:
    """Compose the optional Gmail ingestion worker."""
    if not config.gmail_sync_enabled or config.gmail_transport is None:
        _ = ingestion_lifecycle.activate("gmail")
        return
    triage_runner = EphemeralPiPromptRunner(
        _ephemeral_pi_config(
            bootstrap,
            config=config,
            kb_root=kb_root,
            run_kind="gmail",
            model=model_catalog.default_config,
        )
    )
    sync = GmailSyncService(
        database=database,
        client=GmailClient(transport=config.gmail_transport),
        memory_service=memory_service,
        trigger_service=trigger_service,
        todo_service=todo_service,
        triage_runner=triage_runner,
        triage_batch_size=config.gmail_triage_batch_size,
    )

    async def _boot_gmail() -> IngestionBootOutcome:
        _ = await sync.sync(logger=logger)
        return IngestionBootOutcome.REPEAT

    async def _repeat_gmail() -> None:
        await sync.sync_forever(
            interval_seconds=config.gmail_sync_interval_seconds, logger=logger
        )

    _ = ingestion_lifecycle.activate(
        "gmail", CallbackIngestionWorker(_boot_gmail, _repeat_gmail)
    )


async def _wire_gmail_purge(  # noqa: PLR0913 - composition requires each dependency
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    kb_root: Path,
    logger: Logger,
    model_catalog: AgentModelCatalog,
    proposal_service: ProposalService,
) -> None:
    """Compose the optional Gmail backlog-purge worker."""
    if not config.gmail_purge_enabled or config.gmail_transport is None:
        _ = ingestion_lifecycle.activate("gmail-purge")
        return
    triage_runner = EphemeralPiPromptRunner(
        _ephemeral_pi_config(
            bootstrap,
            config=config,
            kb_root=kb_root,
            run_kind="gmail_purge",
            model=model_catalog.default_config,
        )
    )
    sweep = GmailPurgeSweepService(
        database=database,
        client=GmailClient(transport=config.gmail_transport),
        proposal_service=proposal_service,
        triage_runner=triage_runner,
        chunk_size=config.gmail_purge_chunk_size,
    )

    async def _boot_gmail_purge() -> IngestionBootOutcome:
        _ = await sweep.sweep(logger=logger)
        return IngestionBootOutcome.REPEAT

    async def _repeat_gmail_purge() -> None:
        await sweep.sync_forever(
            interval_seconds=config.gmail_purge_interval_seconds, logger=logger
        )

    _ = ingestion_lifecycle.activate(
        "gmail-purge",
        CallbackIngestionWorker(_boot_gmail_purge, _repeat_gmail_purge),
    )


async def _wire_ebook_stats(
    *,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
) -> None:
    """Compose the optional KOReader statistics-file ingestion worker."""
    if not config.ebook_statistics_db_path:
        _ = ingestion_lifecycle.activate("ebook-statistics")
        return
    sync = EbookStatsSyncService(
        database=database,
        statistics_db_path=Path(config.ebook_statistics_db_path),
    )

    async def _boot_ebook_stats() -> IngestionBootOutcome:
        _ = await sync.sync(logger=logger)
        return IngestionBootOutcome.REPEAT

    async def _repeat_ebook_stats() -> None:
        await sync.sync_forever(
            interval_seconds=config.ebook_statistics_sync_interval_seconds,
            logger=logger,
        )

    _ = ingestion_lifecycle.activate(
        "ebook-statistics",
        CallbackIngestionWorker(_boot_ebook_stats, _repeat_ebook_stats),
    )


async def _wire_ingestion_gates(  # noqa: PLR0913 - composition needs each domain
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    database: Database,
    event_publisher: EventHub,
    ingestion_lifecycle: IngestionLifecycle,
    kb_root: Path,
    logger: Logger,
    memory_service: MemoryService,
    model_catalog: AgentModelCatalog,
    proposal_service: ProposalService,
    resources: contextlib.AsyncExitStack,
    todo_service: TodoService,
    tracer: Tracer,
    trigger_service: TriggerService,
    youtube_search: YouTubeSearchService | None,
) -> YouTubeService:
    """Compose every optional source adapter into one lifecycle owner."""
    youtube = await _wire_youtube(
        config=config,
        database=database,
        event_publisher=event_publisher,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
        tracer=tracer,
        youtube_search=youtube_search,
    )
    await _wire_readwise(
        config=config,
        database=database,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
        memory_service=memory_service,
        resources=resources,
    )
    await _wire_reader(
        config=config,
        database=database,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
        memory_service=memory_service,
        resources=resources,
    )
    await _wire_gmail(
        bootstrap=bootstrap,
        config=config,
        database=database,
        ingestion_lifecycle=ingestion_lifecycle,
        kb_root=kb_root,
        logger=logger,
        memory_service=memory_service,
        model_catalog=model_catalog,
        trigger_service=trigger_service,
        todo_service=todo_service,
    )
    await _wire_gmail_purge(
        bootstrap=bootstrap,
        config=config,
        database=database,
        ingestion_lifecycle=ingestion_lifecycle,
        kb_root=kb_root,
        logger=logger,
        model_catalog=model_catalog,
        proposal_service=proposal_service,
    )
    await _wire_ebook_stats(
        config=config,
        database=database,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
    )
    return youtube.service


def _ephemeral_pi_config(
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


@dataclass(frozen=True, slots=True)
class _SchedulerDependencies:
    """Dependencies required to compose scheduled-trigger delivery."""

    bootstrap: HostBootstrap
    config: AppConfig
    database: Database
    event_hub: EventHub
    kb_root: Path
    logger: Logger
    model_catalog: AgentModelCatalog
    push_service: PushService
    trigger_service: TriggerService


@dataclass(frozen=True, slots=True)
class _SchedulerComponent:
    """Scheduler and the notification service shared with other domains."""

    notification_service: NotificationService
    scheduler: Scheduler


def _build_scheduler(dependencies: _SchedulerDependencies) -> _SchedulerComponent:
    """Wire the Scheduled-trigger scheduler over its dispatch collaborators.

    Agent-prompt triggers spawn ephemeral pi processes under a dedicated session
    root; fixed-message triggers never touch pi. Delivery goes out over the
    in-process event hub as `notify` frames and is persisted through the
    notification service so a fired reminder survives a reload. The typed
    dependency bundle keeps every collaborator explicit at this boundary.
    """
    notification_service = NotificationService(
        database=dependencies.database,
        event_publisher=dependencies.event_hub,
    )
    prompt_runner = EphemeralPiPromptRunner(
        _ephemeral_pi_config(
            dependencies.bootstrap,
            config=dependencies.config,
            kb_root=dependencies.kb_root,
            run_kind="scheduled",
            model=dependencies.model_catalog.default_config,
        )
    )
    notifier: TriggerNotifier = EventNotifier(
        dependencies.event_hub, notification_service
    )
    if (
        dependencies.config.vapid_public_key
        and dependencies.config.vapid_private_key
        and dependencies.config.vapid_subject
    ):
        notifier = PushDeliveryNotifier(
            notifier,
            StoredPushSender(
                push_service=dependencies.push_service,
                transport=VapidWebPushTransport(
                    VapidConfig(
                        private_key=dependencies.config.vapid_private_key,
                        public_key=dependencies.config.vapid_public_key,
                        subject=dependencies.config.vapid_subject,
                    )
                ),
            ),
        )
    scheduler = Scheduler(
        service=dependencies.trigger_service,
        dispatcher=TriggerDispatcher(
            notifier=notifier,
            agent_runner=prompt_runner,
        ),
        clock=SystemClock(),
        logger=dependencies.logger,
        config=SchedulerConfig(
            tick_seconds=dependencies.config.scheduler_tick_seconds,
            concurrency=dependencies.config.scheduler_concurrency,
        ),
    )
    return _SchedulerComponent(
        notification_service=notification_service,
        scheduler=scheduler,
    )


def _build_proposal_service(
    *,
    config: AppConfig,
    database: Database,
    event_publisher: EventHub,
    notification_service: NotificationService,
    tracer: Tracer,
) -> ProposalService:
    """Wire the host proposal executor over agent-composed action sets.

    Built after `_build_scheduler` so its notification service can queue pending
    proposals. The action registry
    carries the Gmail hygiene consumer (`*GMAIL_ACTION_SPECS`); the action
    context's `gmail_client` is the real client when a Gmail transport is
    configured, else `None` — in which case the `gmail.*` executors fail soft
    ("gmail client unavailable") rather than crashing.
    """
    gmail_client = (
        GmailClient(transport=config.gmail_transport)
        if config.gmail_transport is not None
        else None
    )
    return ProposalService(
        database=database,
        tracer=tracer,
        event_publisher=event_publisher,
        action_registry=build_action_registry(all_action_specs()),
        action_context=ActionContext(gmail_client=gmail_client),
        notification_service=notification_service,
    )


@dataclass(frozen=True, slots=True)
class _RecallDependencies:
    """Dependencies required to compose model-backed Recall operations."""

    bootstrap: HostBootstrap
    config: AppConfig
    database: Database
    event_hub: EventHub
    kb_root: Path
    memory_service: MemoryService
    model_catalog: AgentModelCatalog
    tracer: Tracer


def _build_recall_service(dependencies: _RecallDependencies) -> RecallService:
    """Wire the Recall service over its model-backed generator and grader.

    Distilling a transcript into learnings + prompts and judging free-text
    answers are the model steps in Recall, so both run an ephemeral pi under a
    dedicated session root (one shared runner); everything else (deterministic
    grading, scheduling, the completion tether) is pure. Shared collaborators
    arrive through one typed dependency bundle.
    """
    generator: StudyItemGenerator | None = dependencies.config.study_item_generator
    grader: AnswerGrader | None = dependencies.config.answer_grader
    if generator is None or grader is None:
        runner = EphemeralPiPromptRunner(
            _ephemeral_pi_config(
                dependencies.bootstrap,
                config=dependencies.config,
                kb_root=dependencies.kb_root,
                run_kind="recall",
                model=dependencies.model_catalog.default_config,
            )
        )
        generator = generator or PiStudyItemGenerator(runner)
        grader = grader or PiAnswerGrader(runner)
    return RecallService(
        database=dependencies.database,
        memory_service=dependencies.memory_service,
        models=RecallModelSteps(generator=generator, grader=grader),
        event_publisher=dependencies.event_hub,
        tracer=dependencies.tracer,
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
    instead of splitting it across composition statements."""
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


async def _build_todo_service(
    *,
    database: Database,
    event_hub: EventHub,
    memory_service: MemoryService,
    tracer: Tracer,
    logger: Logger,
) -> tuple[TodoService, Callable[[], Awaitable[str]]]:
    """Wire the Todo service, run its one-time backfill, and build its digest.

    Returns the service and async provider of the standing Todo digest block
    appended to conversation runs. The `action:
    pending` facet backfill is idempotent, so running it every boot is safe."""
    todo_service = TodoService(
        database=database, event_publisher=event_hub, tracer=tracer
    )
    _ = await migrate_pending_action_facets(
        database, todo_service, memory_service, logger=logger
    )

    async def _todo_digest() -> str:
        readiness = await todo_service.readiness(now=datetime.now(UTC), logger=logger)
        return render_todo_digest(readiness)

    return todo_service, _todo_digest


@dataclass(frozen=True, slots=True)
class _PresentationComponent:
    """Presentation and KOReader services built from the same dependencies."""

    artifact_service: ArtifactService
    kosync_auth: KosyncAuth
    kosync_service: KosyncService
    panel_service: PanelService


def _build_presentation_services(
    *,
    config: AppConfig,
    database: Database,
    event_hub: EventHub,
    memory_service: MemoryService,
    tracer: Tracer,
) -> _PresentationComponent:
    """Build presentation services and the optional KOReader protocol gate.

    `KosyncService` remains available for owner-facing labeling even when the
    device protocol is disabled. Blank device credentials are harmless because
    the application factory omits those routes when the gate is disabled.
    """
    return _PresentationComponent(
        artifact_service=ArtifactService(
            database=database,
            event_publisher=event_hub,
            tracer=tracer,
        ),
        kosync_auth=KosyncAuth(
            username=config.kosync_username,
            userkey=config.kosync_userkey,
        ),
        kosync_service=KosyncService(
            database=database,
            memory_service=memory_service,
        ),
        panel_service=PanelService(
            database=database,
            memory_service=memory_service,
            event_publisher=event_hub,
            tracer=tracer,
        ),
    )


async def _build_youtube_search(
    *,
    database: Database,
    embedder: Embedder | None,
    index_dir: Path,
) -> tuple[YouTubeSearchService | None, YouTubeSearchReconciler | None]:
    """Wire semantic YouTube corpus Search when an embedder is supplied.

    Opens the YouTube text-chunk index and returns the searcher `YouTubeService`
    uses alongside the reconciler the lifespan drives periodically. Unlike the
    Memory index there is no boot reconcile — a cold pass re-embeds the whole
    saved-video corpus and would block startup, so the periodic loop fills it. With
    no embedder returns `(None, None)`: the index is never opened, search falls
    back to the lexical `LIKE` path, and no model loads."""
    if embedder is None:
        return None, None
    index = await YouTubeSearchIndex.open(
        index_dir=index_dir, vector_dim=embedder.vector_dim
    )
    reconciler = YouTubeSearchReconciler(
        database=database, index=index, embedder=embedder
    )
    searcher = YouTubeSearchService(embedder=embedder, index=index)
    return searcher, reconciler


def _reconcile_loop_tasks(
    *,
    search_reconciler: SearchReconciler | None,
    bucket_item_reconciler: BucketItemReconciler | None,
    youtube_search_reconciler: YouTubeSearchReconciler | None,
    interval_seconds: float,
    logger: Logger,
) -> list[asyncio.Task[None]]:
    """Periodic reconcile loops for the wired search indexes.

    Each loop is the correctness backstop for its index — sweeping orphans and
    running `optimize()` while the host is up. The Memory and Bucket-item loops
    complement their own boot reconcile; the YouTube Search loop has no boot pass,
    so it fills that index shortly after startup. Any of the
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
    if youtube_search_reconciler is not None:
        tasks.append(
            asyncio.create_task(
                youtube_search_reconciler.reconcile_forever(
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


def _wire_provider_auth(
    *,
    config: AppConfig,
    runtime_registry: ConversationRuntimeRegistry,
) -> ProviderAuthService:
    """Wire server-owned provider auth with live-runtime invalidation."""
    return ProviderAuthService(
        config.provider_auth_backend
        or SubprocessProviderAuthBackend(provider_auth_helper_command()),
        on_authorized=runtime_registry.shutdown_all,
    )


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


def _wire_web_search(config: AppConfig, database: Database) -> SearchProvider | None:
    """Attach the persisted monthly spend guard and return configured search."""
    if isinstance(config.search_provider, TavilySearchProvider):
        config.search_provider.spend_guard = PersistentSearchSpendGuard(
            database, max_uses=config.search_max_uses
        )
    return config.search_provider


HOST_QUIET_LOGGERS = (*QUIET_LOGGERS, "aiosqlite", "snekql", "httpcore2")
"""Dependency loggers whose debug chatter obscures host application events."""


@dataclass(frozen=True, slots=True)
class _HostFoundations:
    """Infrastructure shared by every domain component during composition."""

    ingestion_lifecycle: IngestionLifecycle
    kb_root: Path
    logger: Logger
    telemetry: Telemetry


def _build_host_foundations(
    *,
    config: AppConfig,
    telemetry_settings: TelemetrySettings,
) -> _HostFoundations:
    """Configure process-wide infrastructure before domain wiring begins."""
    logger = configure_logging(
        config.logging_level,
        log_file=config.log_file,
        quiet_loggers=HOST_QUIET_LOGGERS,
    )
    telemetry = configure_telemetry(telemetry_settings)
    return _HostFoundations(
        ingestion_lifecycle=IngestionLifecycle(logger),
        kb_root=Path(config.kb_root),
        logger=logger,
        telemetry=telemetry,
    )


async def _compose_app_runtime(  # noqa: PLR0913 - application composition root
    app: FastAPI,
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    embedder: Embedder | None,
    resources: contextlib.AsyncExitStack,
    telemetry_settings: TelemetrySettings,
) -> None:
    """Build and install the complete request-serving dependency graph."""
    foundations = _build_host_foundations(
        config=config, telemetry_settings=telemetry_settings
    )
    _ = resources.callback(foundations.telemetry.shutdown)
    await AsyncPath(foundations.kb_root).mkdir(parents=True, exist_ok=True)
    db, telemetry_db = await resources.enter_async_context(_open_databases(config))
    await _create_schemas(db)
    await create_health_connect_schema(telemetry_db)
    health_connect_ingestion, health_connect_telemetry = (
        HealthConnectIngestion(telemetry_db),
        HealthConnectTelemetry(telemetry_db),
    )
    search_provider = _wire_web_search(config, db)
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
    kb_service = KnowledgeBaseService(kb_root=foundations.kb_root)
    event_hub = EventHub()
    # Search is wired only when an embedder is supplied. Production
    # (`create_app_from_environment`) passes a `FastEmbedder`; tests that
    # exercise search pass a `FakeEmbedder`; everything else runs with
    # search disabled and never opens the index or loads a model.
    search_reconciler = await _build_search(
        database=db,
        embedder=embedder,
        index_dir=foundations.kb_root / "index",
        logger=foundations.logger,
    )
    (
        youtube_searcher,
        youtube_search_reconciler,
    ) = await _build_youtube_search(
        database=db,
        embedder=embedder,
        index_dir=foundations.kb_root / "transcript-index",
    )
    memory_service = MemoryService(
        database=db,
        event_publisher=event_hub,
        kb_service=kb_service,
        tracer=foundations.telemetry.tracer,
        searcher=search_reconciler,
    )
    await memory_service.regenerate_knowledge_base(logger=foundations.logger)
    # The digest reuses the same embedder as search: semantic dedup and
    # contradiction recall when it is wired, keyword fallback when not.
    review_service = ReviewService(database=db, embedder=embedder)
    triage_service = TriageService(database=db)
    bucket_item_reconciler = await _build_bucket_item_search(
        database=db,
        embedder=embedder,
        index_dir=foundations.kb_root / "bucket-item-index",
        logger=foundations.logger,
    )
    (
        bucket_item_service,
        search_fusion_service,
    ) = _build_bucket_item_and_fusion_services(
        database=db,
        event_hub=event_hub,
        memory_service=memory_service,
        searcher=bucket_item_reconciler,
        tracer=foundations.telemetry.tracer,
    )
    presentation = _build_presentation_services(
        config=config,
        database=db,
        event_hub=event_hub,
        memory_service=memory_service,
        tracer=foundations.telemetry.tracer,
    )
    recall_service = _build_recall_service(
        _RecallDependencies(
            bootstrap=bootstrap,
            config=config,
            database=db,
            event_hub=event_hub,
            kb_root=foundations.kb_root,
            memory_service=memory_service,
            model_catalog=model_catalog,
            tracer=foundations.telemetry.tracer,
        )
    )
    conversation_service = ConversationService(
        database=db,
        model_catalog=model_catalog,
    )
    todo_service, todo_digest_provider = await _build_todo_service(
        database=db,
        event_hub=event_hub,
        memory_service=memory_service,
        tracer=foundations.telemetry.tracer,
        logger=foundations.logger,
    )
    runtime_registry = ConversationRuntimeRegistry(
        RuntimeRegistryConfig(
            model_catalog=model_catalog,
            extra_extension_paths=config.extra_extension_paths,
            idle_seconds=config.pi_idle_seconds,
            pi_binary=config.pi_binary,
            session_registry=bootstrap.session_registry,
            session_root=Path(config.pi_session_root)
            if config.pi_session_root is not None
            else foundations.kb_root / "pi-sessions",
            tool_base_url=config.tool_base_url,
            tool_secret=bootstrap.tool_secret,
            todo_digest_provider=todo_digest_provider,
        )
    )
    _ = resources.push_async_callback(runtime_registry.shutdown_all)
    provider_auth_service = _wire_provider_auth(
        config=config,
        runtime_registry=runtime_registry,
    )
    _ = resources.push_async_callback(provider_auth_service.shutdown)
    trigger_service = TriggerService(
        database=db,
        event_publisher=event_hub,
        tracer=foundations.telemetry.tracer,
    )
    push_service = PushService(
        database=db,
        event_publisher=event_hub,
    )
    scheduler_component = _build_scheduler(
        _SchedulerDependencies(
            bootstrap=bootstrap,
            config=config,
            database=db,
            event_hub=event_hub,
            kb_root=foundations.kb_root,
            logger=foundations.logger,
            model_catalog=model_catalog,
            push_service=push_service,
            trigger_service=trigger_service,
        )
    )
    _ = resources.push_async_callback(scheduler_component.scheduler.shutdown)
    proposal_service = _build_proposal_service(
        config=config,
        database=db,
        event_publisher=event_hub,
        notification_service=scheduler_component.notification_service,
        tracer=foundations.telemetry.tracer,
    )
    background_tasks = [
        asyncio.create_task(runtime_registry.reap_idle_forever()),
        asyncio.create_task(scheduler_component.scheduler.run_forever()),
    ]
    # The periodic search-index reconcile loops (Memory + Bucket-item +
    # transcript), each started only when its index was wired (an
    # embedder was supplied).
    background_tasks.extend(
        _reconcile_loop_tasks(
            search_reconciler=search_reconciler,
            bucket_item_reconciler=bucket_item_reconciler,
            youtube_search_reconciler=youtube_search_reconciler,
            interval_seconds=config.search_reconcile_seconds,
            logger=foundations.logger,
        )
    )
    _ = resources.push_async_callback(
        _shutdown_background_tasks,
        background_tasks,
        logger=foundations.logger,
    )
    ingestion_resources = await resources.enter_async_context(
        contextlib.AsyncExitStack()
    )
    _ = resources.push_async_callback(foundations.ingestion_lifecycle.stop)
    youtube_service = await _wire_ingestion_gates(
        bootstrap=bootstrap,
        config=config,
        database=db,
        event_publisher=event_hub,
        ingestion_lifecycle=foundations.ingestion_lifecycle,
        kb_root=foundations.kb_root,
        logger=foundations.logger,
        memory_service=memory_service,
        model_catalog=model_catalog,
        proposal_service=proposal_service,
        resources=ingestion_resources,
        todo_service=todo_service,
        tracer=foundations.telemetry.tracer,
        trigger_service=trigger_service,
        youtube_search=youtube_searcher,
    )
    install_app_runtime(
        app,
        AppRuntime(
            app_password=config.app_password,
            artifact_service=presentation.artifact_service,
            bucket_item_service=bucket_item_service,
            conversation_runtime_registry=runtime_registry,
            conversation_service=conversation_service,
            event_hub=event_hub,
            health_connect_ingestion=health_connect_ingestion,
            health_connect_telemetry=health_connect_telemetry,
            ingestion_lifecycle=foundations.ingestion_lifecycle,
            kosync_auth=presentation.kosync_auth,
            kosync_service=presentation.kosync_service,
            logger=foundations.logger,
            memory_service=memory_service,
            model_catalog=model_catalog,
            notification_service=scheduler_component.notification_service,
            panel_service=presentation.panel_service,
            proposal_service=proposal_service,
            provider_auth_service=provider_auth_service,
            push_service=push_service,
            recall_service=recall_service,
            review_service=review_service,
            search_fusion_service=search_fusion_service,
            search_provider=search_provider,
            secure_cookies=config.secure_cookies,
            session_registry=bootstrap.session_registry,
            session_secret=config.session_secret,
            stt_client=bootstrap.stt_client,
            telemetry=foundations.telemetry,
            todo_service=todo_service,
            tool_secret=bootstrap.tool_secret,
            trace_recorder=bootstrap.trace_recorder,
            triage_service=triage_service,
            trigger_service=trigger_service,
            vapid_public_key=config.vapid_public_key,
            youtube_service=youtube_service,
        ),
    )


def app_lifespan(
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    telemetry_settings: TelemetrySettings,
    embedder: Embedder | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None, bool | None]]:
    """Create lifespan wiring for a configured SQLite DB and KB root.

    `embedder` defaults to the in-host `FastEmbedder` (loads the ONNX model on
    first boot); tests inject a `FakeEmbedder` to keep the search path in the
    gate without a model download."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Build the Memory service for the app lifetime and close it after."""
        async with contextlib.AsyncExitStack() as resources:
            for configured_resource in (
                config.gmail_transport,
                config.transcript_provider,
            ):
                if isinstance(configured_resource, AsyncClosable):
                    _ = resources.push_async_callback(configured_resource.aclose)
            await _compose_app_runtime(
                app,
                bootstrap=bootstrap,
                config=config,
                embedder=embedder,
                resources=resources,
                telemetry_settings=telemetry_settings,
            )
            yield

    return lifespan
