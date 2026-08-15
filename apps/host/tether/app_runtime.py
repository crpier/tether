"""Typed services available for the lifetime of one host application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette

if TYPE_CHECKING:
    from tether.agent_trace import AgentTraceRecorder
    from tether.artifacts import ArtifactService
    from tether.bucket_items import BucketItemService
    from tether.chat_engine import ConversationRuntimeRegistry
    from tether.conversations import ConversationService
    from tether.events import EventHub
    from tether.health_connect import HealthConnectIngestion
    from tether.health_connect_telemetry import HealthConnectTelemetry
    from tether.ingestion_lifecycle import IngestionLifecycle
    from tether.kosync import KosyncService
    from tether.kosync_routes import KosyncAuth
    from tether.memories import MemoryService
    from tether.model_selection import AgentModelCatalog
    from tether.notifications import NotificationService
    from tether.panels import PanelService
    from tether.proposals import ProposalService
    from tether.provider_auth import ProviderAuthService
    from tether.push import PushService
    from tether.recall import RecallService
    from tether.review import ReviewService
    from tether.search_fusion import SearchFusionService
    from tether.search_tools import SearchProvider
    from tether.structured_logging import Logger
    from tether.stt import SttClient
    from tether.telemetry import Telemetry
    from tether.todos import TodoService
    from tether.tools import SessionRegistry
    from tether.triage import TriageService
    from tether.triggers import TriggerService
    from tether.youtube import YouTubeService


@dataclass(frozen=True, slots=True)
class AppRuntime:
    """Complete typed dependency graph exposed while the app accepts requests."""

    app_password: str
    artifact_service: ArtifactService
    bucket_item_service: BucketItemService
    conversation_runtime_registry: ConversationRuntimeRegistry
    conversation_service: ConversationService
    event_hub: EventHub
    health_connect_ingestion: HealthConnectIngestion
    health_connect_telemetry: HealthConnectTelemetry
    ingestion_lifecycle: IngestionLifecycle
    kosync_auth: KosyncAuth
    kosync_service: KosyncService
    logger: Logger
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
    secure_cookies: bool
    session_registry: SessionRegistry
    session_secret: str
    stt_client: SttClient
    telemetry: Telemetry
    todo_service: TodoService
    tool_secret: str
    trace_recorder: AgentTraceRecorder
    triage_service: TriageService
    trigger_service: TriggerService
    vapid_public_key: str
    youtube_service: YouTubeService


def install_app_runtime(app: Starlette, runtime: AppRuntime) -> None:
    """Install the complete runtime as the application's only service graph."""
    app.state.runtime = runtime


def app_runtime(app: Starlette) -> AppRuntime:
    """Return the initialized typed runtime for one application."""
    return cast("AppRuntime", app.state.runtime)
