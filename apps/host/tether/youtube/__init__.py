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
from tether.youtube.service import (
    YouTubeService,
    YouTubeVideoNotFoundError,
)
from tether.youtube.store import (
    IngestedVideo,
    TranscriptPersistedStatus,
    YouTubeTranscriptState,
    create_youtube_schema,
)
from tether.youtube.sync import YouTubeSyncConfig, YouTubeSyncService
from tether.youtube.tools import YOUTUBE_TOOL_SPECS, internal_youtube_tool_routes

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
    "SystemClock",
    "TranscriptPersistedStatus",
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
    "YouTubeTranscriptState",
    "YouTubeVideoNotFoundError",
    "auth_routes_router",
    "create_youtube_schema",
    "internal_youtube_tool_routes",
    "routes_router",
    "state_get",
    "state_set",
]
