"""Composition of request-serving domain services and background loops."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry.trace import Tracer
from snekql.sqlite import Database

from tether.artifacts import ArtifactService
from tether.bucket_item_index import BucketItemIndex
from tether.bucket_item_reconciler import BucketItemReconciler
from tether.bucket_item_search import BucketItemSearchService
from tether.bucket_items import BucketItemService
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.chat_turn import ChatTurnDependencies, ConversationTurnQueue
from tether.conversation_titling import ConversationTitler, PiTitleGenerator
from tether.conversation_turns import ConversationTurns
from tether.conversations import ConversationService
from tether.dreaming import (
    ConversationWindowDreamingExecutor,
    DreamingMutationCoordinator,
    DreamingService,
    DreamingWorker,
    HttpDreamingMutationAcknowledger,
    KindDispatchingDreamExecutor,
    MaintenanceDreamingExecutor,
)
from tether.events import EventHub
from tether.health_connect import (
    HealthEpisodeSummarizer,
    HealthMomentDispatcher,
    HealthMomentObservationQuery,
    HealthMomentService,
    HealthMomentWorker,
    HealthPlanOccurrenceReconciler,
    HealthPlanService,
)
from tether.health_distillation import (
    HealthDistillationExecutor,
    HealthDistillationService,
    HealthDreamingWorker,
)
from tether.host_config import AppConfig
from tether.host_resources import (
    HostBootstrap,
    HostResources,
    ephemeral_pi_config,
    shutdown_background_tasks,
)
from tether.kosync import KosyncService
from tether.kosync_routes import KosyncAuth
from tether.kosync_store import KosyncStore
from tether.memory_workspace_service import (
    MemoryWorkspaceService,
    memory_workspace_root,
)
from tether.model_selection import (
    AgentModelCatalog,
    AgentModelConfig,
    ModelNotAllowedError,
)
from tether.notification_delivery import (
    EventNotifier,
    PushDeliveryNotifier,
    PushSender,
    ScheduledExecutionDependencies,
    TriggerDispatcher,
    TriggerNotifier,
)
from tether.notification_store import NotificationStore
from tether.notifications import NotificationService
from tether.panel_execution import PanelExecutor
from tether.panels import PanelService
from tether.product_observations import ProductObservationService
from tether.provider_auth import ProviderAuthService
from tether.provider_auth_process import (
    SubprocessProviderAuthBackend,
    provider_auth_helper_command,
)
from tether.push import PushService
from tether.push_model import VapidConfig
from tether.push_store import PushStore
from tether.recall import RecallModelSteps, RecallService
from tether.recall_generation import PiStudyItemGenerator, StudyItemGenerator
from tether.recall_grading import AnswerGrader, PiAnswerGrader
from tether.scheduler import (
    EphemeralPiPromptRunner,
    Scheduler,
    SchedulerConfig,
    SystemClock,
)
from tether.search_projection.embeddings import Embedder
from tether.search_projection.metadata import SearchMetaService
from tether.search_spend import PersistentSearchSpendGuard
from tether.structured_logging import Logger
from tether.tavily_search import TavilySearchProvider
from tether.todo_digest import render_todo_digest
from tether.todos import TodoService
from tether.triage import TriageService
from tether.triggers import TriggerService
from tether.web_push import StoredPushSender, VapidWebPushTransport
from tether.web_search import SearchProvider
from tether.youtube import (
    YouTubeSearchIndex,
    YouTubeSearchReconciler,
    YouTubeSearchService,
)


@dataclass(frozen=True, slots=True)
class _SchedulerDependencies:
    """Dependencies required to compose scheduled-trigger delivery."""

    bootstrap: HostBootstrap
    config: AppConfig
    conversation_runtime_registry: ConversationRuntimeRegistry
    conversation_service: ConversationService
    conversation_turn_queue: ConversationTurnQueue
    conversation_turns: ConversationTurns
    database: Database
    dreaming_enabled: bool
    dreaming_service: DreamingService
    event_hub: EventHub
    logger: Logger
    push_service: PushService
    trigger_service: TriggerService


@dataclass(frozen=True, slots=True)
class _SchedulerComponent:
    """Scheduler and notification delivery shared with other domains."""

    notification_service: NotificationService
    prompt_push_sender: PushSender | None
    scheduler: Scheduler


async def _shutdown_scheduled_execution(
    scheduler: Scheduler,
    conversation_turns: ConversationTurns,
) -> None:
    """Stop intake, settle turn owners, then bound scheduler waiter shutdown."""
    scheduler.stop_intake()
    await conversation_turns.shutdown()
    await scheduler.shutdown()


def _build_scheduler(dependencies: _SchedulerDependencies) -> _SchedulerComponent:
    """Wire the Scheduled-trigger scheduler over its dispatch collaborators.

    Agent-prompt triggers run through the default Conversation; fixed-message
    triggers retain durable Inbox delivery. Both action kinds retain configured
    Web Push delivery. The typed dependency bundle keeps every collaborator
    explicit at this boundary.
    """
    notification_service = NotificationService(
        store=NotificationStore(dependencies.database),
        event_publisher=dependencies.event_hub,
    )
    notifier: TriggerNotifier = EventNotifier(
        dependencies.event_hub, notification_service
    )
    prompt_push_sender: PushSender | None = None
    if (
        dependencies.config.vapid_public_key
        and dependencies.config.vapid_private_key
        and dependencies.config.vapid_subject
    ):
        prompt_push_sender = StoredPushSender(
            push_service=dependencies.push_service,
            transport=VapidWebPushTransport(
                VapidConfig(
                    private_key=dependencies.config.vapid_private_key,
                    public_key=dependencies.config.vapid_public_key,
                    subject=dependencies.config.vapid_subject,
                )
            ),
        )
        notifier = PushDeliveryNotifier(notifier, prompt_push_sender)
    scheduler = Scheduler(
        service=dependencies.trigger_service,
        dispatcher=TriggerDispatcher(
            dependencies=ScheduledExecutionDependencies(
                conversation_service=dependencies.conversation_service,
                conversation_turns=dependencies.conversation_turns,
                trigger_service=dependencies.trigger_service,
            ),
            notifier=notifier,
            event_publisher=dependencies.event_hub,
            prompt_push_sender=prompt_push_sender,
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
        prompt_push_sender=prompt_push_sender,
        scheduler=scheduler,
    )


@dataclass(frozen=True, slots=True)
class _TitlerDependencies:
    """Collaborators required to compose first-message auto-titling."""

    bootstrap: HostBootstrap
    config: AppConfig
    conversation_service: ConversationService
    kb_root: Path
    model_catalog: AgentModelCatalog
    logger: Logger


def _build_conversation_titler(
    dependencies: _TitlerDependencies,
) -> ConversationTitler | None:
    """Wire first-message auto-titling over an ephemeral pi one-shot.

    Prefers the configured title model and falls back to the allowlist
    default; returns `None` when no model is available to title with.
    """
    config = dependencies.config
    catalog = dependencies.model_catalog
    title_model: AgentModelConfig | None = None
    if config.conversation_title_model is not None:
        try:
            title_model = catalog.resolve(config.conversation_title_model)
        except ModelNotAllowedError:
            title_model = None
    title_model = title_model or catalog.default_config
    if title_model is None:
        return None
    runner = EphemeralPiPromptRunner(
        ephemeral_pi_config(
            dependencies.bootstrap,
            config=config,
            kb_root=dependencies.kb_root,
            run_kind="titling",
            model=title_model,
        )
    )
    return ConversationTitler(
        conversation_service=dependencies.conversation_service,
        generator=PiTitleGenerator(runner),
        logger=dependencies.logger,
    )


@dataclass(frozen=True, slots=True)
class _RecallDependencies:
    """Dependencies required to compose model-backed Recall operations."""

    bootstrap: HostBootstrap
    config: AppConfig
    database: Database
    event_hub: EventHub
    kb_root: Path
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
            ephemeral_pi_config(
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
        models=RecallModelSteps(generator=generator, grader=grader),
        event_publisher=dependencies.event_hub,
        tracer=dependencies.tracer,
    )


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


def _build_bucket_item_services(
    *,
    database: Database,
    event_hub: EventHub,
    searcher: BucketItemReconciler | None,
    tracer: Tracer,
) -> tuple[BucketItemSearchService, BucketItemService]:
    """Wire Bucket-item mutation and Search services."""
    bucket_item_service = BucketItemService(
        database=database,
        event_publisher=event_hub,
        indexer=searcher,
        tracer=tracer,
    )
    bucket_item_search = BucketItemSearchService(
        database=database,
        searcher=searcher,
        tracer=tracer,
    )
    return bucket_item_search, bucket_item_service


async def _build_todo_service(
    *,
    database: Database,
    event_hub: EventHub,
    tracer: Tracer,
    logger: Logger,
) -> tuple[TodoService, Callable[[], Awaitable[str]]]:
    """Wire the Todo service and its standing foreground digest."""
    todo_service = TodoService(
        database=database, event_publisher=event_hub, tracer=tracer
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
    memory_workspace_service: MemoryWorkspaceService,
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
        kosync_service=KosyncService(store=KosyncStore(database)),
        panel_service=PanelService(
            database=database,
            executor=PanelExecutor(
                database=database,
                workspace_service=memory_workspace_service,
                tracer=tracer,
            ),
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
    bucket_item_reconciler: BucketItemReconciler | None,
    youtube_search_reconciler: YouTubeSearchReconciler | None,
    interval_seconds: float,
    logger: Logger,
) -> list[asyncio.Task[None]]:
    """Periodic reconcile loops for the wired search indexes.

    Each loop is the correctness backstop for its index by sweeping orphans.
    Optional Lance compaction stays outside the live host because it can hold a
    table indefinitely and block searches. The Bucket-item loop complements
    its boot reconcile; the YouTube Search loop has no boot
    pass, so it fills that index shortly after startup. Any loop is absent when
    its index was not wired (no embedder)."""
    tasks: list[asyncio.Task[None]] = []
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


def _wire_web_search(config: AppConfig, database: Database) -> SearchProvider | None:
    """Attach the persisted monthly spend guard and return configured Search."""
    if isinstance(config.search_provider, TavilySearchProvider):
        config.search_provider.spend_guard = PersistentSearchSpendGuard(
            database, max_uses=config.search_max_uses
        )
    return config.search_provider


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


@dataclass(frozen=True, slots=True)
class CoreServices:
    """Request-serving services composed before optional ingestion adapters."""

    artifact_service: ArtifactService
    bucket_item_search_service: BucketItemSearchService
    bucket_item_service: BucketItemService
    conversation_runtime_registry: ConversationRuntimeRegistry
    conversation_service: ConversationService
    conversation_turn_queue: ConversationTurnQueue
    conversation_turns: ConversationTurns
    event_hub: EventHub
    kosync_auth: KosyncAuth
    kosync_service: KosyncService
    memory_workspace_service: MemoryWorkspaceService
    model_catalog: AgentModelCatalog
    notification_service: NotificationService
    panel_service: PanelService
    product_observation_service: ProductObservationService
    provider_auth_service: ProviderAuthService
    push_service: PushService
    recall_service: RecallService
    scheduler: Scheduler
    search_provider: SearchProvider | None
    todo_service: TodoService
    triage_service: TriageService
    trigger_service: TriggerService
    dreaming_service: DreamingService
    health_distillation_service: HealthDistillationService
    health_moment_service: HealthMomentService
    health_plan_service: HealthPlanService
    youtube_search: YouTubeSearchService | None


async def compose_core_services(
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    embedder: Embedder | None,
    host: HostResources,
    resources: contextlib.AsyncExitStack,
) -> CoreServices:
    """Compose core services and register every owned background resource."""
    search_provider = _wire_web_search(config, host.database)
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
    event_hub = EventHub()
    dreaming_mutation_coordinator = DreamingMutationCoordinator(
        host.database,
        memory_workspace_root(host.kb_root),
    )
    memory_workspace_service = MemoryWorkspaceService(
        kb_root=host.kb_root,
        reconciler=dreaming_mutation_coordinator,
    )
    _ = await memory_workspace_service.scan(logger=host.logger)

    (
        youtube_searcher,
        youtube_search_reconciler,
    ) = await _build_youtube_search(
        database=host.database,
        embedder=embedder,
        index_dir=host.kb_root / "transcript-index",
    )
    triage_service = TriageService(database=host.database)
    bucket_item_reconciler = await _build_bucket_item_search(
        database=host.database,
        embedder=embedder,
        index_dir=host.kb_root / "bucket-item-index",
        logger=host.logger,
    )
    bucket_item_search, bucket_item_service = _build_bucket_item_services(
        database=host.database,
        event_hub=event_hub,
        searcher=bucket_item_reconciler,
        tracer=host.telemetry.tracer,
    )
    presentation = _build_presentation_services(
        config=config,
        database=host.database,
        event_hub=event_hub,
        memory_workspace_service=memory_workspace_service,
        tracer=host.telemetry.tracer,
    )
    recall_service = _build_recall_service(
        _RecallDependencies(
            bootstrap=bootstrap,
            config=config,
            database=host.database,
            event_hub=event_hub,
            kb_root=host.kb_root,
            model_catalog=model_catalog,
            tracer=host.telemetry.tracer,
        )
    )
    conversation_service = ConversationService(
        database=host.database,
        event_publisher=event_hub,
        model_catalog=model_catalog,
    )
    todo_service, todo_digest_provider = await _build_todo_service(
        database=host.database,
        event_hub=event_hub,
        tracer=host.telemetry.tracer,
        logger=host.logger,
    )
    conversation_turn_queue = ConversationTurnQueue()
    runtime_registry = ConversationRuntimeRegistry(
        RuntimeRegistryConfig(
            model_catalog=model_catalog,
            extra_extension_paths=config.extra_extension_paths,
            idle_seconds=config.pi_idle_seconds,
            pi_binary=config.pi_binary,
            session_registry=bootstrap.session_registry,
            session_root=Path(config.pi_session_root)
            if config.pi_session_root is not None
            else host.kb_root / "pi-sessions",
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
    dreaming_service = DreamingService(
        host.database,
        tracer=host.telemetry.tracer,
        workspace_root=memory_workspace_root(host.kb_root),
    )
    titler = _build_conversation_titler(
        _TitlerDependencies(
            bootstrap=bootstrap,
            config=config,
            conversation_service=conversation_service,
            kb_root=host.kb_root,
            model_catalog=model_catalog,
            logger=host.logger,
        )
    )
    conversation_turns = ConversationTurns(
        ChatTurnDependencies(
            conversation_service=conversation_service,
            dreaming_enabled=config.dreaming_enabled,
            dreaming_service=dreaming_service,
            logger=host.logger,
            runtime_registry=runtime_registry,
            titler=titler,
            trace_recorder=bootstrap.trace_recorder,
            turn_queue=conversation_turn_queue,
        )
    )
    trigger_service = TriggerService(
        database=host.database,
        conversation_turns=conversation_turns,
        event_publisher=event_hub,
        model_catalog=model_catalog,
        tracer=host.telemetry.tracer,
    )
    await trigger_service.migrate_legacy_targets(
        (await conversation_service.fetch_main_conversation()).id
    )
    _ = await trigger_service.repair_occurrences(now=datetime.now(UTC))
    _ = await conversation_turns.repair(datetime.now(UTC))
    health_distillation_service = HealthDistillationService(
        host.database, host.telemetry_database
    )
    _ = await dreaming_mutation_coordinator.reconcile_workspace(logger=host.logger)
    push_service = PushService(
        store=PushStore(host.database),
        event_publisher=event_hub,
    )
    scheduler_component = _build_scheduler(
        _SchedulerDependencies(
            bootstrap=bootstrap,
            config=config,
            conversation_runtime_registry=runtime_registry,
            conversation_service=conversation_service,
            conversation_turn_queue=conversation_turn_queue,
            conversation_turns=conversation_turns,
            database=host.database,
            dreaming_enabled=config.dreaming_enabled,
            dreaming_service=dreaming_service,
            event_hub=event_hub,
            logger=host.logger,
            push_service=push_service,
            trigger_service=trigger_service,
        )
    )
    _ = resources.push_async_callback(
        _shutdown_scheduled_execution,
        scheduler_component.scheduler,
        conversation_turns,
    )
    await scheduler_component.scheduler.repair()
    health_plan_service = HealthPlanService(host.database)
    health_moment_service = HealthMomentService(
        database=host.database,
        observations=HealthMomentObservationQuery(
            host.telemetry_database,
            planned_exercise=HealthPlanOccurrenceReconciler(
                database=host.database,
                telemetry_database=host.telemetry_database,
            ),
        ),
    )
    health_moment_worker = HealthMomentWorker(
        dispatcher=HealthMomentDispatcher(
            conversation_service=conversation_service,
            conversation_turns=conversation_turns,
            database=host.database,
            push_sender=scheduler_component.prompt_push_sender,
        ),
        service=health_moment_service,
        summarizer=HealthEpisodeSummarizer(host.telemetry_database),
    )
    background_tasks = [
        asyncio.create_task(runtime_registry.reap_idle_forever()),
        asyncio.create_task(
            health_moment_worker.run_forever(
                interval_seconds=config.health_episode_sweep_seconds,
                logger=host.logger,
            )
        ),
    ]
    if config.dreaming_enabled:
        dreaming_runner = EphemeralPiPromptRunner(
            replace(
                ephemeral_pi_config(
                    bootstrap,
                    config=config,
                    kb_root=host.kb_root,
                    run_kind="dreaming",
                    model=model_catalog.default_config,
                ),
                load_tether_tools=False,
            )
        )
        dream_mutation_acknowledger = HttpDreamingMutationAcknowledger(
            base_url=config.tool_base_url,
            tool_secret=bootstrap.tool_secret,
        )
        window_executor = ConversationWindowDreamingExecutor(
            conversation_service,
            memory_workspace_service.workspace_root,
            mutation_coordinator=dreaming_mutation_coordinator,
            mutation_acknowledger=dream_mutation_acknowledger,
            curation_runner=dreaming_runner,
        )
        maintenance_executor = MaintenanceDreamingExecutor(
            host.database,
            memory_workspace_service.workspace_root,
            mutation_coordinator=dreaming_mutation_coordinator,
            mutation_acknowledger=dream_mutation_acknowledger,
            consolidation_runner=dreaming_runner,
        )
        background_tasks.append(
            asyncio.create_task(
                DreamingWorker(
                    dreaming_service,
                    KindDispatchingDreamExecutor(
                        {
                            "assimilation": window_executor,
                            "manual": window_executor,
                            "maintenance": maintenance_executor,
                        }
                    ),
                    logger=host.logger,
                ).run_forever()
            )
        )
        # Post-turn queueing only fires on the next chat turn; this scan is
        # the backstop that assimilates settled evidence during quiet spells.
        background_tasks.append(
            asyncio.create_task(dreaming_service.scan_forever(logger=host.logger))
        )
        # Plan 507 §5: periodic maintenance consolidates fragmented topic files
        # into fewer, larger documents and dedupes claims across runs.
        background_tasks.append(
            asyncio.create_task(
                dreaming_service.maintenance_forever(
                    interval_seconds=config.dream_maintenance_interval_seconds,
                    logger=host.logger,
                )
            )
        )
        # Health consolidation (ADR-0016 bespoke sibling): bounded Distillations
        # over Health Connect episode summaries into the same Memory workspace.
        health_dreaming_worker = HealthDreamingWorker(
            health_distillation_service,
            HealthDistillationExecutor(
                host.telemetry_database,
                memory_workspace_service.workspace_root,
                mutation_coordinator=dreaming_mutation_coordinator,
                mutation_acknowledger=HttpDreamingMutationAcknowledger(
                    base_url=config.tool_base_url,
                    tool_secret=bootstrap.tool_secret,
                ),
                curation_runner=dreaming_runner,
            ),
            logger=host.logger,
        )
        background_tasks.append(
            asyncio.create_task(health_dreaming_worker.run_forever())
        )
        background_tasks.append(
            asyncio.create_task(
                health_distillation_service.scan_forever(logger=host.logger)
            )
        )
    # The periodic search-index reconcile loops (Memory + Bucket-item +
    # transcript), each started only when its index was wired (an
    # embedder was supplied).
    background_tasks.extend(
        _reconcile_loop_tasks(
            bucket_item_reconciler=bucket_item_reconciler,
            youtube_search_reconciler=youtube_search_reconciler,
            interval_seconds=config.search_reconcile_seconds,
            logger=host.logger,
        )
    )
    _ = resources.push_async_callback(
        shutdown_background_tasks,
        background_tasks,
        logger=host.logger,
    )
    return CoreServices(
        artifact_service=presentation.artifact_service,
        bucket_item_search_service=bucket_item_search,
        bucket_item_service=bucket_item_service,
        conversation_runtime_registry=runtime_registry,
        conversation_service=conversation_service,
        conversation_turn_queue=conversation_turn_queue,
        conversation_turns=conversation_turns,
        event_hub=event_hub,
        kosync_auth=presentation.kosync_auth,
        kosync_service=presentation.kosync_service,
        memory_workspace_service=memory_workspace_service,
        model_catalog=model_catalog,
        notification_service=scheduler_component.notification_service,
        panel_service=presentation.panel_service,
        product_observation_service=ProductObservationService(
            host.database, event_publisher=event_hub
        ),
        provider_auth_service=provider_auth_service,
        push_service=push_service,
        recall_service=recall_service,
        scheduler=scheduler_component.scheduler,
        search_provider=search_provider,
        todo_service=todo_service,
        triage_service=triage_service,
        trigger_service=trigger_service,
        dreaming_service=dreaming_service,
        health_distillation_service=health_distillation_service,
        health_moment_service=health_moment_service,
        health_plan_service=health_plan_service,
        youtube_search=youtube_searcher,
    )
