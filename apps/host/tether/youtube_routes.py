"""HTTP routes for YouTube ingestion.

Each handler adapts one `YouTubeService` capability to HTTP: `endpoint`
validates the query string with Pydantic, the handler calls
`request.app.state.youtube_service`, and the result is serialised as a
`YouTubeVideoRead` (or a list/transcript response that also carries the call's
quota + cache metadata, mirroring the tool envelope). Domain exceptions
translate to status codes at this boundary — `YouTubeVideoNotFoundError` and
`TranscriptUnavailableError` -> 404, `EmptyYouTubeSearchQueryError` -> 400, and a
depleted quota budget (`YouTubeQuotaExceededError`) -> 429.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime

from pydantic import UUID7, BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.openapi import EndpointRoute, endpoint
from tether.youtube import (
    BrowseResult,
    CacheMeta,
    EmptyYouTubeSearchQueryError,
    IngestedVideo,
    IngestState,
    QuotaMeta,
    SearchResult,
    TranscriptResult,
    TranscriptUnavailableError,
    YouTubeQuotaExceededError,
    YouTubeSource,
    YouTubeVideoNotFoundError,
    derive_ingest_state,
)
from tether.youtube import Fetched as YouTubeFetched


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
    def from_video(cls, video: IngestedVideo[YouTubeFetched]) -> YouTubeVideoRead:
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


def _request_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _path_video_id(request: Request) -> str:
    """Return the `{video_id}` path segment."""
    return request.path_params["video_id"]


def _translate_domain_errors(
    handler: Callable[..., Awaitable[Response]],
) -> Callable[..., Awaitable[Response]]:
    """Map YouTube ingestion failures onto HTTP status codes at the boundary.

    Absence (an unknown video, or no upstream transcript) is a 404; a blank
    keyword query is a 400; a depleted quota budget is a 429.
    """

    @functools.wraps(handler)
    async def translated(*arguments: object) -> Response:
        try:
            return await handler(*arguments)
        except YouTubeVideoNotFoundError, TranscriptUnavailableError:
            return JSONResponse({"detail": "youtube video not found"}, status_code=404)
        except EmptyYouTubeSearchQueryError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        except YouTubeQuotaExceededError as error:
            return JSONResponse({"detail": str(error)}, status_code=429)

    return translated


@endpoint(query=BrowseYouTubeQuery, response=YouTubeVideoListResponse)
@_translate_domain_errors
async def browse_youtube(request: Request, query: BrowseYouTubeQuery) -> Response:
    """List active ingested videos, optionally filtered by topic and source."""
    result = await request.app.state.youtube_service.browse(
        topic=query.topic,
        source=query.source,
        logger=_request_logger(request),
    )
    return JSONResponse(
        YouTubeVideoListResponse.from_result(result).model_dump(mode="json")
    )


@endpoint(query=SearchYouTubeQuery, response=YouTubeVideoListResponse)
@_translate_domain_errors
async def search_youtube(request: Request, query: SearchYouTubeQuery) -> Response:
    """Keyword Search across saved content and transcript text."""
    result = await request.app.state.youtube_service.search(
        query.q,
        logger=_request_logger(request),
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
        logger=_request_logger(request),
    )
    return JSONResponse(
        YouTubeTranscriptResponse.from_result(result).model_dump(mode="json")
    )


@endpoint(response=YouTubeVideoRead)
@_translate_domain_errors
async def ignore_youtube_video(request: Request) -> Response:
    """Purge a video from ingestion."""
    video = await request.app.state.youtube_service.ignore(
        _path_video_id(request),
        logger=_request_logger(request),
    )
    return JSONResponse(YouTubeVideoRead.from_video(video).model_dump(mode="json"))


@endpoint(response=YouTubeVideoRead)
@_translate_domain_errors
async def retry_youtube_video(request: Request) -> Response:
    """Un-ignore a previously purged video."""
    video = await request.app.state.youtube_service.retry(
        _path_video_id(request),
        logger=_request_logger(request),
    )
    return JSONResponse(YouTubeVideoRead.from_video(video).model_dump(mode="json"))


# `/api/youtube/search` precedes `/api/youtube/{video_id}/...` so the literal
# path wins.
youtube_routes: list[Route] = [
    EndpointRoute("/api/youtube", browse_youtube, methods=["GET"]),
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
