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

from tether.capability_contracts import ErrorRule
from tether.transcripts import (
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
    YouTubeService,
)
from tether.youtube.store import (
    IngestedVideo,
    IngestState,
    YouTubeSource,
    derive_ingest_state,
)
from tether.youtube.transcription_service import (
    TranscriptBlockedError,
    TranscriptNeedsReviewError,
    TranscriptRequestResult,
    TranscriptResult,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeVideoNotFoundError,
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
        case Err(TranscriptNeedsReview(target=target)):
            raise TranscriptNeedsReviewError(target.locator)
        case Err(TranscriptExplicitlyUnavailable(target=target)):
            raise TranscriptUnavailableError(target.locator)
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
    created_at: datetime
    updated_at: datetime
    ignored_at: datetime | None
    liked_at: datetime | None
    duration_seconds: int | None

    @classmethod
    def from_video(cls, video: IngestedVideo[Fetched]) -> YouTubeVideoRead:
        """Render stored YouTube metadata without Transcription state."""
        return cls(
            id=video.id,
            video_id=video.video_id,
            source=video.source,
            state=derive_ingest_state(video),
            title=video.title,
            channel=video.channel,
            topic=video.topic,
            description=video.description,
            created_at=video.created_at,
            updated_at=video.updated_at,
            ignored_at=video.ignored_at,
            liked_at=video.liked_at,
            duration_seconds=video.duration_seconds,
        )
