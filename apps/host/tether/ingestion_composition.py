"""Composition of optional ingestion adapters under one lifecycle owner."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from opentelemetry.trace import Tracer
from snekok import Err
from snekql.sqlite import Database

from tether.ebook_stats import EbookStatsSyncService
from tether.ebook_stats_store import EbookStatsStore
from tether.events import EventHub
from tether.gmail import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailSyncService,
    GoogleGmailAuthService,
)
from tether.gmail_purge import GmailPurgeSweepService
from tether.host_config import AppConfig
from tether.host_resources import HostBootstrap, ephemeral_pi_config
from tether.ingestion_lifecycle import (
    CallbackIngestionWorker,
    IngestionBootOutcome,
    IngestionLifecycle,
)
from tether.model_selection import AgentModelCatalog
from tether.proposals import ProposalService
from tether.reader import ReaderClient, ReaderSyncService
from tether.readwise import ReadwiseClient, ReadwiseSyncService
from tether.readwise_http import (
    HttpReaderTransport,
    HttpReadwiseTransport,
    ReadwiseAuthenticationFailure,
)
from tether.scheduler import EphemeralPiPromptRunner
from tether.structured_logging import Logger
from tether.todos import TodoService
from tether.transcripts.acquisition import TranscriptAcquisitionService
from tether.transcripts.contracts import TranscriptProviderChain
from tether.transcripts.provider_health import load_all_provider_pauses
from tether.transcripts.worker import TranscriptSyncService
from tether.triggers import TriggerService
from tether.youtube import (
    DailyQuota,
    InMemoryYouTubeApi,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeAuthService,
    YouTubeSearchService,
    YouTubeService,
    YouTubeSyncConfig,
    YouTubeSyncService,
)
from tether.youtube import SystemClock as YouTubeSystemClock


@dataclass(frozen=True, slots=True)
class YouTubeComponent:
    """YouTube authorization, request service, and worker readiness."""

    auth_service: YouTubeAuthService
    likes_ready: asyncio.Event
    service: YouTubeService
    transcripts_ready: asyncio.Event


@dataclass(frozen=True, slots=True)
class _YouTubeWorkerDependencies:
    """Collaborators shared by the independently optional YouTube workers."""

    auth_service: YouTubeAuthService
    config: AppConfig
    ingestion_lifecycle: IngestionLifecycle
    logger: Logger
    sync: YouTubeSyncService
    transcript_sync: TranscriptSyncService | None


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


async def compose_youtube(  # noqa: PLR0913 - composition requires each dependency
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
        provider_pauses=load_all_provider_pauses,
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

    async def _sync_after_authorization() -> None:
        try:
            _ = await sync.sync(logger=logger)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("YouTube post-authorization sync failed")

    auth_service = YouTubeAuthService(
        config.youtube_auth_backend,
        on_authorized=(
            _sync_after_authorization if config.youtube_sync_enabled else None
        ),
    )
    worker_dependencies = _YouTubeWorkerDependencies(
        auth_service=auth_service,
        config=config,
        ingestion_lifecycle=ingestion_lifecycle,
        logger=logger,
        sync=sync,
        transcript_sync=transcript_sync,
    )
    return YouTubeComponent(
        auth_service=auth_service,
        likes_ready=_activate_youtube_likes(worker_dependencies),
        service=youtube_service,
        transcripts_ready=_activate_youtube_transcripts(worker_dependencies),
    )


def _activate_youtube_likes(
    dependencies: _YouTubeWorkerDependencies,
) -> asyncio.Event:
    """Run likes only while Google authorization is usable."""
    worker: CallbackIngestionWorker | None = None
    if (
        dependencies.config.youtube_api is not None
        and dependencies.config.youtube_sync_enabled
    ):

        async def _boot() -> IngestionBootOutcome:
            if not await dependencies.auth_service.available():
                return IngestionBootOutcome.REPEAT
            _ = await dependencies.sync.maybe_sync(logger=dependencies.logger)
            return IngestionBootOutcome.REPEAT

        async def _repeat() -> None:
            while True:
                await asyncio.sleep(dependencies.config.youtube_sync_interval_seconds)
                if not await dependencies.auth_service.available():
                    continue
                try:
                    _ = await dependencies.sync.sync(logger=dependencies.logger)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    dependencies.logger.exception("YouTube sync pass failed")

        worker = CallbackIngestionWorker(_boot, _repeat)
    return dependencies.ingestion_lifecycle.activate("youtube-likes", worker)


def _activate_youtube_transcripts(
    dependencies: _YouTubeWorkerDependencies,
) -> asyncio.Event:
    """Run transcript acquisition only when every provider seam is configured."""
    worker: CallbackIngestionWorker | None = None
    if (
        dependencies.transcript_sync is not None
        and dependencies.config.youtube_api is not None
        and dependencies.config.transcript_provider is not None
        and dependencies.config.transcript_sync_enabled
    ):

        async def _boot() -> IngestionBootOutcome:
            if not await dependencies.auth_service.available():
                return IngestionBootOutcome.REPEAT
            assert dependencies.transcript_sync is not None
            _ = await dependencies.transcript_sync.sync(logger=dependencies.logger)
            return IngestionBootOutcome.REPEAT

        async def _repeat() -> None:
            assert dependencies.transcript_sync is not None
            while True:
                await asyncio.sleep(
                    dependencies.config.transcript_sync_interval_seconds
                )
                if not await dependencies.auth_service.available():
                    continue
                try:
                    _ = await dependencies.transcript_sync.sync(
                        logger=dependencies.logger
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    dependencies.logger.exception("YouTube transcript sync pass failed")

        worker = CallbackIngestionWorker(_boot, _repeat)
    return dependencies.ingestion_lifecycle.activate(
        "youtube-transcripts",
        worker,
    )


async def compose_readwise(
    *,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
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
    sync = ReadwiseSyncService(database=database, client=client)

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


async def compose_reader(
    *,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    logger: Logger,
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


async def compose_gmail(  # noqa: PLR0913, C901 - each param and branch count are intentional
    *,
    bootstrap: HostBootstrap,
    config: AppConfig,
    database: Database,
    ingestion_lifecycle: IngestionLifecycle,
    kb_root: Path,
    logger: Logger,
    model_catalog: AgentModelCatalog,
    trigger_service: TriggerService,
    todo_service: TodoService,
    gmail_client: GmailClient | None = None,
    gmail_auth_service: GoogleGmailAuthService | None = None,
) -> None:
    """Compose the optional Gmail ingestion worker."""
    if not config.gmail_sync_enabled:
        _ = ingestion_lifecycle.activate("gmail")
        return
    client = gmail_client or (
        GmailClient(config.gmail_transport)
        if config.gmail_transport is not None
        else None
    )
    if client is None:
        _ = ingestion_lifecycle.activate("gmail")
        return
    triage_runner = EphemeralPiPromptRunner(
        ephemeral_pi_config(
            bootstrap,
            config=config,
            kb_root=kb_root,
            run_kind="gmail",
            model=model_catalog.default_config,
        )
    )
    sync = GmailSyncService(
        database=database,
        client=client,
        trigger_service=trigger_service,
        todo_service=todo_service,
        triage_runner=triage_runner,
        triage_batch_size=config.gmail_triage_batch_size,
    )

    async def _boot_gmail() -> IngestionBootOutcome:
        if gmail_auth_service is not None and not await gmail_auth_service.available():
            return IngestionBootOutcome.REPEAT
        report = await sync.sync(logger=logger)
        if isinstance(report, Err):
            logger.warning(
                "Gmail boot sync failed",
                failure=type(report.error).__name__,
                operation=report.error.operation,
            )
            if isinstance(report.error, GmailAuthenticationFailure):
                return IngestionBootOutcome.STOP
        return IngestionBootOutcome.REPEAT

    async def _repeat_gmail() -> None:
        while True:
            await asyncio.sleep(config.gmail_sync_interval_seconds)
            if (
                gmail_auth_service is not None
                and not await gmail_auth_service.available()
            ):
                continue
            try:
                _ = await sync.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gmail sync pass failed")

    _ = ingestion_lifecycle.activate(
        "gmail", CallbackIngestionWorker(_boot_gmail, _repeat_gmail)
    )


async def compose_gmail_purge(  # noqa: PLR0913 - composition requires each dependency
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
        ephemeral_pi_config(
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
        report = await sweep.sweep(logger=logger)
        if isinstance(report, Err):
            logger.warning(
                "Gmail purge boot sweep failed",
                failure=type(report.error).__name__,
                operation=report.error.operation,
            )
            if isinstance(report.error, GmailAuthenticationFailure):
                return IngestionBootOutcome.STOP
        return IngestionBootOutcome.REPEAT

    async def _repeat_gmail_purge() -> None:
        await sweep.sync_forever(
            interval_seconds=config.gmail_purge_interval_seconds, logger=logger
        )

    _ = ingestion_lifecycle.activate(
        "gmail-purge",
        CallbackIngestionWorker(_boot_gmail_purge, _repeat_gmail_purge),
    )


async def compose_ebook_stats(
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
        store=EbookStatsStore(database),
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


@dataclass(frozen=True, slots=True)
class IngestionDependencies:
    """Explicit collaborators shared across optional ingestion adapters."""

    bootstrap: HostBootstrap
    config: AppConfig
    database: Database
    event_hub: EventHub
    ingestion_lifecycle: IngestionLifecycle
    kb_root: Path
    logger: Logger
    model_catalog: AgentModelCatalog
    proposal_service: ProposalService
    todo_service: TodoService
    tracer: Tracer
    trigger_service: TriggerService
    youtube_search: YouTubeSearchService | None
    gmail_client: GmailClient | None = None
    gmail_auth_service: GoogleGmailAuthService | None = None


async def compose_ingestion(
    dependencies: IngestionDependencies,
    *,
    resources: contextlib.AsyncExitStack,
) -> YouTubeComponent:
    """Compose every optional source adapter into one lifecycle owner."""
    youtube = await compose_youtube(
        config=dependencies.config,
        database=dependencies.database,
        event_publisher=dependencies.event_hub,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        logger=dependencies.logger,
        tracer=dependencies.tracer,
        youtube_search=dependencies.youtube_search,
    )
    await compose_readwise(
        config=dependencies.config,
        database=dependencies.database,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        logger=dependencies.logger,
        resources=resources,
    )
    await compose_reader(
        config=dependencies.config,
        database=dependencies.database,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        logger=dependencies.logger,
        resources=resources,
    )
    await compose_gmail(
        bootstrap=dependencies.bootstrap,
        config=dependencies.config,
        database=dependencies.database,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        kb_root=dependencies.kb_root,
        logger=dependencies.logger,
        model_catalog=dependencies.model_catalog,
        trigger_service=dependencies.trigger_service,
        todo_service=dependencies.todo_service,
        gmail_client=dependencies.gmail_client,
        gmail_auth_service=dependencies.gmail_auth_service,
    )
    await compose_gmail_purge(
        bootstrap=dependencies.bootstrap,
        config=dependencies.config,
        database=dependencies.database,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        kb_root=dependencies.kb_root,
        logger=dependencies.logger,
        model_catalog=dependencies.model_catalog,
        proposal_service=dependencies.proposal_service,
    )
    await compose_ebook_stats(
        config=dependencies.config,
        database=dependencies.database,
        ingestion_lifecycle=dependencies.ingestion_lifecycle,
        logger=dependencies.logger,
    )
    return youtube
