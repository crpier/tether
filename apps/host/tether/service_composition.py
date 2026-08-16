"""Composition of request-serving domain services and background loops."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry.trace import Tracer
from snekql.sqlite import Database

from tether.action_registry import (
    ActionContext,
    all_action_specs,
    build_action_registry,
)
from tether.artifacts import ArtifactService
from tether.bucket_item_index import BucketItemIndex
from tether.bucket_item_reconciler import BucketItemReconciler
from tether.bucket_item_search import BucketItemSearchService
from tether.bucket_items import BucketItemService
from tether.chat_engine import ConversationRuntimeRegistry, RuntimeRegistryConfig
from tether.conversations import ConversationService
from tether.embeddings import Embedder
from tether.events import EventHub
from tether.gmail_client import GmailClient
from tether.host_config import AppConfig
from tether.host_resources import (
    HostBootstrap,
    HostResources,
    ephemeral_pi_config,
    shutdown_background_tasks,
)
from tether.kosync import KosyncService
from tether.kosync_routes import KosyncAuth
from tether.memories import MemoryService
from tether.memory_projection import KnowledgeBaseService
from tether.memory_search import MemorySearchService
from tether.model_selection import AgentModelCatalog
from tether.notifications import NotificationService
from tether.panels import PanelService
from tether.proposals import ProposalService
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
)
from tether.recall import RecallModelSteps, RecallService
from tether.recall_generation import PiStudyItemGenerator, StudyItemGenerator
from tether.recall_grading import AnswerGrader, PiAnswerGrader
from tether.reconciler import SearchReconciler
from tether.review import ReviewService
from tether.scheduler import (
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
from tether.search_meta import SearchMetaService
from tether.search_tools import (
    PersistentSearchSpendGuard,
    SearchProvider,
    TavilySearchProvider,
)
from tether.structured_logging import Logger
from tether.todo_digest import render_todo_digest
from tether.todos import TodoService, migrate_pending_action_facets
from tether.triage import TriageService
from tether.triggers import TriggerService
from tether.youtube_search import YouTubeSearchService
from tether.youtube_search_index import YouTubeSearchIndex
from tether.youtube_search_reconciler import YouTubeSearchReconciler


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
        ephemeral_pi_config(
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
    memory_search: MemorySearchService,
    searcher: BucketItemReconciler | None,
    tracer: Tracer,
) -> tuple[BucketItemSearchService, BucketItemService, SearchFusionService]:
    """Wire the Bucket-item service and the cross-source fusion service above it.

    Fusion depends on both the Bucket-item and Memory services existing, so
    building them together keeps that dependency explicit at the one call site
    instead of splitting it across composition statements."""
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
    search_fusion_service = SearchFusionService(
        bucket_item_search=bucket_item_search,
        memory_search=memory_search,
    )
    return bucket_item_search, bucket_item_service, search_fusion_service


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


def _build_presentation_services(  # noqa: PLR0913 - each service is explicit
    *,
    config: AppConfig,
    database: Database,
    event_hub: EventHub,
    memory_search: MemorySearchService,
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
            memory_search=memory_search,
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
    event_hub: EventHub
    kosync_auth: KosyncAuth
    kosync_service: KosyncService
    memory_search_service: MemorySearchService
    memory_service: MemoryService
    model_catalog: AgentModelCatalog
    notification_service: NotificationService
    panel_service: PanelService
    proposal_service: ProposalService
    provider_auth_service: ProviderAuthService
    push_service: PushService
    recall_service: RecallService
    review_service: ReviewService
    search_fusion_service: SearchFusionService
    search_provider: SearchProvider | None
    todo_service: TodoService
    triage_service: TriageService
    trigger_service: TriggerService
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
    kb_service = KnowledgeBaseService(kb_root=host.kb_root)
    event_hub = EventHub()
    # Search is wired only when an embedder is supplied. Production
    # (`create_app_from_environment`) passes a `FastEmbedder`; tests that
    # exercise search pass a `FakeEmbedder`; everything else runs with
    # search disabled and never opens the index or loads a model.
    search_reconciler = await _build_search(
        database=host.database,
        embedder=embedder,
        index_dir=host.kb_root / "index",
        logger=host.logger,
    )
    (
        youtube_searcher,
        youtube_search_reconciler,
    ) = await _build_youtube_search(
        database=host.database,
        embedder=embedder,
        index_dir=host.kb_root / "transcript-index",
    )
    memory_service = MemoryService(
        database=host.database,
        event_publisher=event_hub,
        indexer=search_reconciler,
        kb_service=kb_service,
        tracer=host.telemetry.tracer,
    )
    memory_search = MemorySearchService(
        database=host.database,
        searcher=search_reconciler,
        tracer=host.telemetry.tracer,
    )
    await memory_service.regenerate_knowledge_base(logger=host.logger)
    # The digest reuses the same embedder as search: semantic dedup and
    # contradiction recall when it is wired, keyword fallback when not.
    review_service = ReviewService(database=host.database, embedder=embedder)
    triage_service = TriageService(database=host.database)
    bucket_item_reconciler = await _build_bucket_item_search(
        database=host.database,
        embedder=embedder,
        index_dir=host.kb_root / "bucket-item-index",
        logger=host.logger,
    )
    (
        bucket_item_search,
        bucket_item_service,
        search_fusion_service,
    ) = _build_bucket_item_and_fusion_services(
        database=host.database,
        event_hub=event_hub,
        memory_search=memory_search,
        searcher=bucket_item_reconciler,
        tracer=host.telemetry.tracer,
    )
    presentation = _build_presentation_services(
        config=config,
        database=host.database,
        event_hub=event_hub,
        memory_search=memory_search,
        memory_service=memory_service,
        tracer=host.telemetry.tracer,
    )
    recall_service = _build_recall_service(
        _RecallDependencies(
            bootstrap=bootstrap,
            config=config,
            database=host.database,
            event_hub=event_hub,
            kb_root=host.kb_root,
            memory_service=memory_service,
            model_catalog=model_catalog,
            tracer=host.telemetry.tracer,
        )
    )
    conversation_service = ConversationService(
        database=host.database,
        model_catalog=model_catalog,
    )
    todo_service, todo_digest_provider = await _build_todo_service(
        database=host.database,
        event_hub=event_hub,
        memory_service=memory_service,
        tracer=host.telemetry.tracer,
        logger=host.logger,
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
    trigger_service = TriggerService(
        database=host.database,
        event_publisher=event_hub,
        tracer=host.telemetry.tracer,
    )
    push_service = PushService(
        database=host.database,
        event_publisher=event_hub,
    )
    scheduler_component = _build_scheduler(
        _SchedulerDependencies(
            bootstrap=bootstrap,
            config=config,
            database=host.database,
            event_hub=event_hub,
            kb_root=host.kb_root,
            logger=host.logger,
            model_catalog=model_catalog,
            push_service=push_service,
            trigger_service=trigger_service,
        )
    )
    _ = resources.push_async_callback(scheduler_component.scheduler.shutdown)
    proposal_service = _build_proposal_service(
        config=config,
        database=host.database,
        event_publisher=event_hub,
        notification_service=scheduler_component.notification_service,
        tracer=host.telemetry.tracer,
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
        event_hub=event_hub,
        kosync_auth=presentation.kosync_auth,
        kosync_service=presentation.kosync_service,
        memory_search_service=memory_search,
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
        todo_service=todo_service,
        triage_service=triage_service,
        trigger_service=trigger_service,
        youtube_search=youtube_searcher,
    )
