"""YouTube Integration interface (ADR-0025).

The only import surface for Tether code outside this package. Everything else
in ``tether.youtube`` is an internal seam owned by this Integration and its
tests.
"""

from tether.capability_contracts import CacheMeta, QuotaMeta
from tether.youtube.auth_routes import router as auth_routes_router
from tether.youtube.auth_service import (
    GoogleYouTubeAuthBackend,
    ReauthorizableYouTubeApi,
    YouTubeAuthBackend,
    YouTubeAuthFailure,
    YouTubeAuthorization,
    YouTubeAuthService,
)
from tether.youtube.local import InMemoryYouTubeApi
from tether.youtube.oauth import YOUTUBE_READONLY_SCOPE
from tether.youtube.quota import (
    Clock,
    DailyQuota,
    SystemClock,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeSyncState,
    state_get,
    state_set,
)
from tether.youtube.routes import router as routes_router
from tether.youtube.search import YouTubeSearchService
from tether.youtube.search_index import YouTubeSearchIndex
from tether.youtube.search_reconciler import YouTubeSearchReconciler
from tether.youtube.service import YouTubeService
from tether.youtube.store import (
    IngestedVideo,
    create_youtube_schema,
    migrate_legacy_youtube_transcripts,
    remove_legacy_youtube_transcript_storage,
)
from tether.youtube.sync import YouTubeSyncConfig, YouTubeSyncService
from tether.youtube.tools import YOUTUBE_TOOL_SPECS, internal_youtube_tool_routes
from tether.youtube.transcript_sources import (
    SupadataSourceConfig,
    TranscriptLibrarySourceConfig,
    TranscriptProviderConfig,
    build_configured_transcript_provider,
)
from tether.youtube.transcript_sync import TranscriptSyncConfig, TranscriptSyncService
from tether.youtube.transcription_service import (
    TranscriptBlockedError,
    TranscriptDecision,
    TranscriptDecisionOutcome,
    TranscriptNeedsReviewError,
    TranscriptRequestResult,
    TranscriptResult,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeTranscriptionService,
    YouTubeVideoNotFoundError,
)
from tether.youtube.types import VideoId

__all__ = [
    "YOUTUBE_READONLY_SCOPE",
    "YOUTUBE_TOOL_SPECS",
    "CacheMeta",
    "Clock",
    "DailyQuota",
    "GoogleYouTubeAuthBackend",
    "InMemoryYouTubeApi",
    "IngestedVideo",
    "QuotaMeta",
    "ReauthorizableYouTubeApi",
    "SupadataSourceConfig",
    "SystemClock",
    "TranscriptBlockedError",
    "TranscriptDecision",
    "TranscriptDecisionOutcome",
    "TranscriptLibrarySourceConfig",
    "TranscriptNeedsReviewError",
    "TranscriptProviderConfig",
    "TranscriptRequestResult",
    "TranscriptResult",
    "TranscriptSyncConfig",
    "TranscriptSyncService",
    "TranscriptTransientError",
    "TranscriptUnavailableError",
    "VideoId",
    "YouTubeApi",
    "YouTubeApiClient",
    "YouTubeApiGate",
    "YouTubeApiGateConfig",
    "YouTubeAuthBackend",
    "YouTubeAuthFailure",
    "YouTubeAuthService",
    "YouTubeAuthorization",
    "YouTubeSearchIndex",
    "YouTubeSearchReconciler",
    "YouTubeSearchService",
    "YouTubeService",
    "YouTubeSyncConfig",
    "YouTubeSyncService",
    "YouTubeSyncState",
    "YouTubeTranscriptionService",
    "YouTubeVideoNotFoundError",
    "auth_routes_router",
    "build_configured_transcript_provider",
    "create_youtube_schema",
    "internal_youtube_tool_routes",
    "migrate_legacy_youtube_transcripts",
    "remove_legacy_youtube_transcript_storage",
    "routes_router",
    "state_get",
    "state_set",
]
