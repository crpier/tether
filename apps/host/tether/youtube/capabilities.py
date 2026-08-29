"""The YouTube ingestion domain's capability descriptor.

The pieces the REST routes (`tether.youtube.routes`) and the internal tools
(`tether.youtube.tools`) both need live here once: the `YouTubeVideoRead`
model, the domain→code map (`YOUTUBE_ERRORS`), and the executes whose payload
is identical on both surfaces (ignore/retry). Browse, search, and transcript
fetch keep per-surface bodies — the tool seam serves deliberately compact,
context-budgeted rows while REST serves full read models — but both translate
failures through the same table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from pydantic import UUID7, BaseModel
from snekok.result import Err, Ok
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.structured_logging import get_request_logger
from tether.transcripts.contracts import (
    TranscriptAcquisitionDeferred,
    TranscriptExplicitlyUnavailable,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptRetryScheduled,
)
from tether.youtube.quota import YouTubeQuotaExceededError
from tether.youtube.service import (
    EmptyYouTubeSearchQueryError,
    InvalidYouTubeActivityRangeError,
    TranscriptBlockedError,
    TranscriptNeedsReviewError,
    TranscriptRequestResult,
    TranscriptResult,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeService,
    YouTubeVideoNotFoundError,
)
from tether.youtube.store import (
    IngestedVideo,
    IngestState,
    TranscriptStatus,
    YouTubeSource,
    derive_ingest_state,
)


class _YouTubeCapabilitiesRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    youtube_service: YouTubeService


def _runtime(request: Request) -> _YouTubeCapabilitiesRuntime:
    """Read the installed application runtime off the request."""
    return cast("_YouTubeCapabilitiesRuntime", request.app.state.runtime)


YOUTUBE_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule(
        (TranscriptNeedsReviewError,),
        "transcript_needs_review",
        409,
        detail="transcript acquisition needs human review",
    ),
    ErrorRule(
        (TranscriptUnavailableError,),
        "transcript_unavailable",
        404,
        detail="transcript is explicitly unavailable",
    ),
    ErrorRule(
        (YouTubeVideoNotFoundError,),
        "not_found",
        404,
        detail="youtube video not found",
    ),
    ErrorRule(
        (EmptyYouTubeSearchQueryError, InvalidYouTubeActivityRangeError),
        "invalid_input",
        400,
    ),
    ErrorRule((YouTubeQuotaExceededError,), "quota_exceeded", 429),
    ErrorRule(
        (TranscriptTransientError, TranscriptBlockedError), "upstream_error", 503
    ),
)
"""The YouTube domain→code map both surfaces translate failures through.

An unknown video is ``not_found``; provider exhaustion is
``transcript_needs_review``; human-settled absence is ``transcript_unavailable``;
a blank Search is invalid input; depleted quota is 429; transient/provider blocks
are 503.
"""


class YouTubeCapabilityInvariantError(Exception):
    """Raised when a typed capability result violates its closed result union."""


def unwrap_transcript_request(outcome: TranscriptRequestResult) -> TranscriptResult:
    """Translate typed transcript request failures at an application boundary."""
    match outcome:
        case Ok(result):
            return result
        case Err(TranscriptNeedsReview(video_id=video_id)):
            raise TranscriptNeedsReviewError(video_id)
        case Err(TranscriptExplicitlyUnavailable(video_id=video_id)):
            raise TranscriptUnavailableError(video_id)
        case Err(TranscriptProviderBlocked(source=source)):
            message = f"transcript provider {source} is blocked"
            raise TranscriptBlockedError(message, source=source)
        case Err(TranscriptRetryScheduled()):
            message = "transcript acquisition is temporarily unavailable"
            raise TranscriptTransientError(message)
        case Err(TranscriptAcquisitionDeferred()):
            message = "transcript acquisition is temporarily unavailable"
            raise TranscriptTransientError(message)
        case _:
            message = "unhandled transcript request result"
            raise YouTubeCapabilityInvariantError(message)


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
    ...     transcript_status="pending",
    ...     created_at=datetime(2026, 1, 1),
    ...     updated_at=datetime(2026, 1, 1),
    ...     ignored_at=None,
    ...     liked_at=None,
    ...     duration_seconds=None,
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
    transcript_status: TranscriptStatus
    created_at: datetime
    updated_at: datetime
    ignored_at: datetime | None
    liked_at: datetime | None
    duration_seconds: int | None

    @classmethod
    def from_video(
        cls,
        video: IngestedVideo[Fetched],
        *,
        transcript_status: TranscriptStatus,
    ) -> YouTubeVideoRead:
        """Render a stored video with its normalized transcript status."""
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
            transcript_status=transcript_status,
            created_at=video.created_at,
            updated_at=video.updated_at,
            ignored_at=video.ignored_at,
            liked_at=video.liked_at,
            duration_seconds=video.duration_seconds,
        )


async def _single(request: Request, video: IngestedVideo[Fetched]) -> CapabilityOutcome:
    """Render a single ingested video (ignore/retry carry no quota/cache)."""
    transcript_status = await _runtime(request).youtube_service.transcript_status(
        video.video_id
    )
    return CapabilityOutcome(
        result=YouTubeVideoRead.from_video(
            video, transcript_status=transcript_status
        ).model_dump(mode="json")
    )


async def ignore(request: Request, video_id: str) -> CapabilityOutcome:
    """Purge a video from ingestion."""
    video = await _runtime(request).youtube_service.ignore(
        video_id,
        logger=get_request_logger(request),
    )
    return await _single(request, video)


async def retry(request: Request, video_id: str) -> CapabilityOutcome:
    """Return a previously purged video to ingestion."""
    video = await _runtime(request).youtube_service.retry(
        video_id,
        logger=get_request_logger(request),
    )
    return await _single(request, video)
