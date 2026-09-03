"""HTTP routes for YouTube ingestion.

Each handler adapts one `YouTubeService` capability to HTTP: `endpoint`
validates the query string with Pydantic, the handler calls
`_runtime(request).youtube_service` (or, for ignore/retry, binds the path id
onto the shared execute in `tether.youtube.capabilities`), and the result is
serialised as a `YouTubeVideoRead` (or a list/transcript response that also
carries the call's quota + cache metadata, mirroring the tool envelope).
Domain exceptions translate to status codes through the domain's `ErrorRule`
table (`YOUTUBE_ERRORS`) — the same table the internal tool surface maps onto
envelope codes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, RootModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.capabilities import translate_domain_errors
from tether.structured_logging import get_request_logger
from tether.transcripts import TranscriptionStatus
from tether.youtube.capabilities import YOUTUBE_ERRORS, YouTubeVideoRead
from tether.youtube.capabilities import (
    unwrap_transcript_request as _unwrap_transcript_request,
)
from tether.youtube.quota import QuotaMeta
from tether.youtube.service import (
    BrowseResult,
    CacheMeta,
    SearchResult,
    YouTubeService,
    YouTubeSyncStatus,
)
from tether.youtube.store import YouTubeSource
from tether.youtube.transcription_service import (
    TranscriptDecision,
    TranscriptResult,
    YouTubeTranscriptionService,
)


class _YouTubeRoutesRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    youtube_service: YouTubeService
    youtube_transcription_service: YouTubeTranscriptionService


def _runtime(request: Request) -> _YouTubeRoutesRuntime:
    """Read the installed application runtime off the request."""
    return cast("_YouTubeRoutesRuntime", request.app.state.runtime)


class BrowseYouTubeQuery(BaseModel):
    """Query string for a topic/source-filtered browse.

    >>> BrowseYouTubeQuery().topic is None
    True
    """

    topic: str | None = None
    source: YouTubeSource | None = None


class SearchYouTubeQuery(BaseModel):
    """Query string for keyword Search over ingested videos.

    >>> SearchYouTubeQuery(q="async").q
    'async'
    """

    q: str


class YouTubeVideoListResponse(BaseModel):
    """A browse/search result: the videos plus the call's quota + cache."""

    videos: list[YouTubeVideoRead]
    quota: QuotaMeta
    cache: CacheMeta

    @classmethod
    def from_result(
        cls, result: BrowseResult | SearchResult
    ) -> YouTubeVideoListResponse:
        """Render a browse/search result as its HTTP representation."""
        return cls(
            videos=[YouTubeVideoRead.from_video(video) for video in result.videos],
            quota=result.quota,
            cache=result.cache,
        )


class YouTubeTranscriptResponse(BaseModel):
    """Transcript text fetched through a YouTube media association."""

    transcript: str
    cache: CacheMeta

    @classmethod
    def from_result(cls, result: TranscriptResult) -> YouTubeTranscriptResponse:
        """Render a transcript result as its HTTP representation."""
        return cls(
            transcript=result.transcript,
            cache=result.cache,
        )


class TranscriptProviderPauseRead(BaseModel):
    """HTTP representation of a transcript source paused by an IP block."""

    source: str
    paused_until: datetime


class YouTubeTranscriptionStatusRead(BaseModel):
    """Transcription progress for the associated YouTube corpus."""

    done: int
    pending: int
    needs_review: int
    unavailable: int
    providers_paused: list[TranscriptProviderPauseRead]


class YouTubeSyncStatusRead(BaseModel):
    """HTTP snapshot keeping YouTube and Transcription state distinct."""

    videos_total: int
    last_synced_at: datetime | None
    quota: QuotaMeta
    api_paused_until: datetime | None
    transcriptions: YouTubeTranscriptionStatusRead

    @classmethod
    def from_status(cls, status: YouTubeSyncStatus) -> YouTubeSyncStatusRead:
        """Render a service sync-status snapshot as its HTTP representation."""
        return cls(
            videos_total=status.videos_total,
            last_synced_at=status.last_synced_at,
            quota=status.quota,
            api_paused_until=status.api_paused_until,
            transcriptions=YouTubeTranscriptionStatusRead(
                done=status.transcriptions.done,
                pending=status.transcriptions.pending,
                needs_review=status.transcriptions.needs_review,
                unavailable=status.transcriptions.unavailable,
                providers_paused=[
                    TranscriptProviderPauseRead(
                        source=pause.source,
                        paused_until=pause.paused_until,
                    )
                    for pause in status.transcriptions.providers_paused
                ],
            ),
        )


class TranscriptDecisionRead(BaseModel):
    """A transcript failure awaiting the human's decision."""

    video_id: str
    title: str
    channel: str
    transcript_status: TranscriptionStatus = "needs_review"
    last_error: str | None
    attempts: int

    @classmethod
    def from_decision(cls, decision: TranscriptDecision) -> TranscriptDecisionRead:
        return cls(
            video_id=decision.video.video_id,
            title=decision.video.title,
            channel=decision.video.channel,
            last_error=decision.last_error,
            attempts=decision.attempts,
        )


class TranscriptDecisionListResponse(RootModel[list[TranscriptDecisionRead]]):
    """Pending transcript decisions."""


class TranscriptDecisionOutcomeRead(BaseModel):
    """The transcript status after a human decision."""

    video_id: str
    transcript_status: TranscriptionStatus


_translate_domain_errors = translate_domain_errors(YOUTUBE_ERRORS)


router = APIRouter()


@router.get("/api/youtube", response_model=YouTubeVideoListResponse)
@_translate_domain_errors
async def browse_youtube(
    request: Request, query: Annotated[BrowseYouTubeQuery, Query()]
) -> Response:
    """List active ingested videos, optionally filtered by topic and source."""
    result = await _runtime(request).youtube_service.browse(
        topic=query.topic,
        source=query.source,
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeVideoListResponse.from_result(result).model_dump(mode="json")
    )


@router.get("/api/youtube/status", response_model=YouTubeSyncStatusRead)
async def youtube_sync_status(request: Request) -> Response:
    """Report the background ingestion's progress and health (local read only)."""
    status = await _runtime(request).youtube_service.sync_status(
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeSyncStatusRead.from_status(status).model_dump(mode="json")
    )


@router.get("/api/youtube/search", response_model=YouTubeVideoListResponse)
@_translate_domain_errors
async def search_youtube(
    request: Request, query: Annotated[SearchYouTubeQuery, Query()]
) -> Response:
    """Keyword Search across saved content and transcript text."""
    result = await _runtime(request).youtube_service.search(
        query.q,
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeVideoListResponse.from_result(result).model_dump(mode="json")
    )


@router.get(
    "/api/youtube/transcript-decisions", response_model=TranscriptDecisionListResponse
)
async def transcript_decisions(request: Request) -> Response:
    """List transcript failures awaiting a human decision."""
    decisions = await _runtime(request).youtube_transcription_service.decisions(
        logger=get_request_logger(request)
    )
    body = TranscriptDecisionListResponse(
        [TranscriptDecisionRead.from_decision(item) for item in decisions]
    )
    return JSONResponse(body.model_dump(mode="json"))


@router.post(
    "/api/youtube/{video_id}/transcript", response_model=YouTubeTranscriptResponse
)
@_translate_domain_errors
async def fetch_youtube_transcript(request: Request, video_id: str) -> Response:
    """Fetch and persist a transcript for an ingested video."""
    outcome = await _runtime(request).youtube_transcription_service.fetch(
        video_id,
        logger=get_request_logger(request),
    )
    result = _unwrap_transcript_request(outcome)
    return JSONResponse(
        YouTubeTranscriptResponse.from_result(result).model_dump(mode="json")
    )


@router.post(
    "/api/youtube/{video_id}/transcript-decision/keep-trying",
    response_model=TranscriptDecisionOutcomeRead,
)
@_translate_domain_errors
async def keep_trying_transcript(request: Request, video_id: str) -> Response:
    """Return a review-needed transcript to pending acquisition."""
    outcome = await _runtime(request).youtube_transcription_service.keep_trying(
        video_id, logger=get_request_logger(request)
    )
    body = TranscriptDecisionOutcomeRead(
        video_id=outcome.video_id, transcript_status=outcome.transcript_status
    )
    return JSONResponse(body.model_dump())


@router.post(
    "/api/youtube/{video_id}/transcript-decision/give-up",
    response_model=TranscriptDecisionOutcomeRead,
)
@_translate_domain_errors
async def give_up_transcript(request: Request, video_id: str) -> Response:
    """Confirm that a review-needed video has no transcript worth pursuing."""
    outcome = await _runtime(request).youtube_transcription_service.give_up(
        video_id, logger=get_request_logger(request)
    )
    body = TranscriptDecisionOutcomeRead(
        video_id=outcome.video_id, transcript_status=outcome.transcript_status
    )
    return JSONResponse(body.model_dump())
