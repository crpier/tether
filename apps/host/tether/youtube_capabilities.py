"""The YouTube ingestion domain's capability descriptor.

The pieces the REST routes (`tether.youtube_routes`) and the internal tools
(`tether.youtube_tools`) both need live here once: the `YouTubeVideoRead`
model, the domain→code map (`YOUTUBE_ERRORS`), and the executes whose payload
is identical on both surfaces (ignore/retry). Browse, search, and transcript
fetch keep per-surface bodies — the tool seam serves deliberately compact,
context-budgeted rows while REST serves full read models — but both translate
failures through the same table.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import UUID7, BaseModel
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.youtube import (
    EmptyYouTubeSearchQueryError,
    Fetched,
    IngestedVideo,
    IngestState,
    TranscriptBlockedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeQuotaExceededError,
    YouTubeSource,
    YouTubeVideoNotFoundError,
    derive_ingest_state,
)

YOUTUBE_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule(
        (YouTubeVideoNotFoundError, TranscriptUnavailableError),
        "not_found",
        404,
        detail="youtube video not found",
    ),
    ErrorRule((EmptyYouTubeSearchQueryError,), "invalid_input", 400),
    ErrorRule((YouTubeQuotaExceededError,), "quota_exceeded", 429),
    ErrorRule(
        (TranscriptTransientError, TranscriptBlockedError), "upstream_error", 503
    ),
)
"""The YouTube domain→code map both surfaces translate failures through.

Absence (an unknown video, or a permanently unavailable/excluded transcript) is
a 404; a blank keyword query is a 400; a depleted quota budget is a 429; a
transient transcript failure or a blocked provider is a 503 (retry later).
"""


class YouTubeVideoRead(BaseModel):
    """HTTP representation of an ingested video, exposing its derived state.

    >>> read = YouTubeVideoRead(
    ...     id="018f0000-0000-7000-8000-000000000000",
    ...     video_id="v1",
    ...     source="liked",
    ...     state="active",
    ...     title="Talk",
    ...     channel="PyConf",
    ...     topic="python",
    ...     description="",
    ...     transcript=None,
    ...     created_at=datetime(2026, 1, 1),
    ...     updated_at=datetime(2026, 1, 1),
    ...     ignored_at=None,
    ... )
    >>> read.state
    'active'
    """

    id: UUID7
    video_id: str
    source: YouTubeSource
    state: IngestState
    title: str
    channel: str
    topic: str
    description: str
    transcript: str | None
    created_at: datetime
    updated_at: datetime
    ignored_at: datetime | None

    @classmethod
    def from_video(cls, video: IngestedVideo[Fetched]) -> YouTubeVideoRead:
        """Render a stored ingested video as its HTTP representation."""
        return cls(
            id=video.id,
            video_id=video.video_id,
            source=video.source,
            state=derive_ingest_state(video),
            title=video.title,
            channel=video.channel,
            topic=video.topic,
            description=video.description,
            transcript=video.transcript,
            created_at=video.created_at,
            updated_at=video.updated_at,
            ignored_at=video.ignored_at,
        )


def _single(video: IngestedVideo[Fetched]) -> CapabilityOutcome:
    """Render a single ingested video (ignore/retry carry no quota/cache)."""
    return CapabilityOutcome(
        result=YouTubeVideoRead.from_video(video).model_dump(mode="json")
    )


async def ignore(request: Request, video_id: str) -> CapabilityOutcome:
    """Purge a video from ingestion."""
    video = await request.app.state.youtube_service.ignore(
        video_id,
        logger=get_request_logger(request),
    )
    return _single(video)


async def retry(request: Request, video_id: str) -> CapabilityOutcome:
    """Return a previously purged video to ingestion."""
    video = await request.app.state.youtube_service.retry(
        video_id,
        logger=get_request_logger(request),
    )
    return _single(video)
