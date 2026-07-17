"""HTTP routes for YouTube ingestion.

Each handler adapts one `YouTubeService` capability to HTTP: `endpoint`
validates the query string with Pydantic, the handler calls
`request.app.state.youtube_service` (or, for ignore/retry, binds the path id
onto the shared execute in `tether.youtube_capabilities`), and the result is
serialised as a `YouTubeVideoRead` (or a list/transcript response that also
carries the call's quota + cache metadata, mirroring the tool envelope).
Domain exceptions translate to status codes through the domain's `ErrorRule`
table (`YOUTUBE_ERRORS`) — the same table the internal tool surface maps onto
envelope codes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether import youtube_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.logging import get_request_logger
from tether.openapi import EndpointRoute, endpoint
from tether.youtube import (
    BrowseResult,
    CacheMeta,
    QuotaMeta,
    SearchResult,
    TranscriptResult,
    YouTubeSource,
    YouTubeSyncStatus,
)
from tether.youtube_capabilities import YOUTUBE_ERRORS, YouTubeVideoRead


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
    """A fetched transcript: the updated video, its text, and quota + cache."""

    video: YouTubeVideoRead
    transcript: str
    quota: QuotaMeta
    cache: CacheMeta

    @classmethod
    def from_result(cls, result: TranscriptResult) -> YouTubeTranscriptResponse:
        """Render a transcript result as its HTTP representation."""
        return cls(
            video=YouTubeVideoRead.from_video(result.video),
            transcript=result.transcript,
            quota=result.quota,
            cache=result.cache,
        )


class TranscriptProviderPauseRead(BaseModel):
    """HTTP representation of a transcript source paused by an IP block."""

    source: str
    paused_until: datetime


class SourceUsageRead(BaseModel):
    """HTTP representation of one transcript source's own metered-use budget.

    Distinct from `quota` (the YouTube Data API's per-day budget, shared by
    every source that calls it): a source with its own cap (e.g. Supadata's
    monthly budget) reports one of these, keyed by its source name on
    `YouTubeSyncStatusRead.usage`. `period` is the UTC calendar month
    (`YYYY-MM`) Supadata's `used`/`remaining` apply to; a source with no
    natural period concept leaves it empty.
    """

    used: int
    limit: int
    remaining: int
    period: str = ""


class YouTubeSyncStatusRead(BaseModel):
    """HTTP snapshot of the background ingestion's progress and health.

    >>> read = YouTubeSyncStatusRead(
    ...     videos_total=3,
    ...     transcripts_done=1,
    ...     transcripts_pending=1,
    ...     transcripts_unavailable=1,
    ...     last_synced_at=None,
    ...     quota=QuotaMeta(limit=10, used=0, remaining=10),
    ...     api_paused_until=None,
    ...     transcript_providers_paused=[],
    ...     usage={},
    ... )
    >>> read.videos_total
    3
    """

    videos_total: int
    transcripts_done: int
    transcripts_pending: int
    transcripts_unavailable: int
    last_synced_at: datetime | None
    quota: QuotaMeta
    api_paused_until: datetime | None
    transcript_providers_paused: list[TranscriptProviderPauseRead]
    usage: dict[str, SourceUsageRead] = {}

    @classmethod
    def from_status(cls, status: YouTubeSyncStatus) -> YouTubeSyncStatusRead:
        """Render a service sync-status snapshot as its HTTP representation."""
        return cls(
            videos_total=status.videos_total,
            transcripts_done=status.transcripts_done,
            transcripts_pending=status.transcripts_pending,
            transcripts_unavailable=status.transcripts_unavailable,
            last_synced_at=status.last_synced_at,
            quota=status.quota,
            api_paused_until=status.api_paused_until,
            transcript_providers_paused=[
                TranscriptProviderPauseRead(
                    source=pause.source, paused_until=pause.paused_until
                )
                for pause in status.transcript_providers_paused
            ],
            usage={
                source: SourceUsageRead(
                    used=usage.used,
                    limit=usage.limit,
                    remaining=usage.remaining,
                    period=usage.period,
                )
                for source, usage in status.usage.items()
            },
        )


def _path_video_id(request: Request) -> str:
    """Return the `{video_id}` path segment."""
    return request.path_params["video_id"]


_translate_domain_errors = translate_domain_errors(YOUTUBE_ERRORS)


@endpoint(query=BrowseYouTubeQuery, response=YouTubeVideoListResponse)
@_translate_domain_errors
async def browse_youtube(request: Request, query: BrowseYouTubeQuery) -> Response:
    """List active ingested videos, optionally filtered by topic and source."""
    result = await request.app.state.youtube_service.browse(
        topic=query.topic,
        source=query.source,
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeVideoListResponse.from_result(result).model_dump(mode="json")
    )


@endpoint(response=YouTubeSyncStatusRead)
async def youtube_sync_status(request: Request) -> Response:
    """Report the background ingestion's progress and health (local read only)."""
    status = await request.app.state.youtube_service.sync_status(
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeSyncStatusRead.from_status(status).model_dump(mode="json")
    )


@endpoint(query=SearchYouTubeQuery, response=YouTubeVideoListResponse)
@_translate_domain_errors
async def search_youtube(request: Request, query: SearchYouTubeQuery) -> Response:
    """Keyword Search across saved content and transcript text."""
    result = await request.app.state.youtube_service.search(
        query.q,
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeVideoListResponse.from_result(result).model_dump(mode="json")
    )


@endpoint(response=YouTubeTranscriptResponse)
@_translate_domain_errors
async def fetch_youtube_transcript(request: Request) -> Response:
    """Fetch and persist a transcript for an ingested video."""
    result = await request.app.state.youtube_service.fetch_transcript(
        _path_video_id(request),
        logger=get_request_logger(request),
    )
    return JSONResponse(
        YouTubeTranscriptResponse.from_result(result).model_dump(mode="json")
    )


@endpoint(response=YouTubeVideoRead)
@_translate_domain_errors
async def ignore_youtube_video(request: Request) -> Response:
    """Purge a video from ingestion."""
    outcome = await youtube_capabilities.ignore(request, _path_video_id(request))
    return rest_response(outcome)


@endpoint(response=YouTubeVideoRead)
@_translate_domain_errors
async def retry_youtube_video(request: Request) -> Response:
    """Un-ignore a previously purged video."""
    outcome = await youtube_capabilities.retry(request, _path_video_id(request))
    return rest_response(outcome)


# `/api/youtube/search` precedes `/api/youtube/{video_id}/...` so the literal
# path wins.
youtube_routes: list[Route] = [
    EndpointRoute("/api/youtube", browse_youtube, methods=["GET"]),
    EndpointRoute("/api/youtube/status", youtube_sync_status, methods=["GET"]),
    EndpointRoute("/api/youtube/search", search_youtube, methods=["GET"]),
    EndpointRoute(
        "/api/youtube/{video_id}/transcript",
        fetch_youtube_transcript,
        methods=["POST"],
    ),
    EndpointRoute(
        "/api/youtube/{video_id}/ignore", ignore_youtube_video, methods=["POST"]
    ),
    EndpointRoute(
        "/api/youtube/{video_id}/retry", retry_youtube_video, methods=["POST"]
    ),
]
