"""The internal YouTube ingestion tool surface, over the shared envelope.

These mount alongside the Memory and Bucket item tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and domain-error translation
(`tether.tools`).

Unlike the Memory and Bucket tools, these front an external, quota-metered API,
so their envelopes populate the `quota` and `cache` fields: every browse,
search, and transcript fetch reports the remaining budget after its guarded call
and whether the result was served live or from cache (story 71).
"""

from __future__ import annotations

from pydantic import BaseModel
from starlette.requests import Request
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.tools import ToolEndpoint, ToolEnvelope, ToolRoute
from tether.youtube import (
    BrowseResult,
    Fetched,
    IngestedVideo,
    SearchResult,
    TranscriptResult,
    YouTubeSource,
)
from tether.youtube_routes import YouTubeVideoRead


class BrowseYouTubeParams(BaseModel):
    """Params for a topic/source-filtered browse of ingested videos."""

    topic: str | None = None
    source: YouTubeSource | None = None


class SearchYouTubeParams(BaseModel):
    """Params for keyword Search across saved content and transcript text."""

    q: str


class FetchYouTubeTranscriptParams(BaseModel):
    """Params for fetching and persisting a video's transcript."""

    video_id: str


class IgnoreYouTubeVideoParams(BaseModel):
    """Params for purging a video from ingestion."""

    video_id: str


class RetryYouTubeVideoParams(BaseModel):
    """Params for returning a previously purged video to ingestion."""

    video_id: str


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _ok_videos(result: BrowseResult | SearchResult) -> ToolEnvelope:
    """Envelope a video collection with the call's quota + cache metadata."""
    return ToolEnvelope(
        success=True,
        result=[
            YouTubeVideoRead.from_video(video).model_dump(mode="json")
            for video in result.videos
        ],
        quota=result.quota,
        cache=result.cache,
    )


def _ok_transcript(result: TranscriptResult) -> ToolEnvelope:
    """Envelope a fetched transcript with the call's quota + cache metadata."""
    return ToolEnvelope(
        success=True,
        result={
            "video": YouTubeVideoRead.from_video(result.video).model_dump(mode="json"),
            "transcript": result.transcript,
        },
        quota=result.quota,
        cache=result.cache,
    )


def _ok_video(video: IngestedVideo[Fetched]) -> ToolEnvelope:
    """Envelope a single ingested video (ignore/retry carry no quota/cache)."""
    return ToolEnvelope(
        success=True,
        result=YouTubeVideoRead.from_video(video).model_dump(mode="json"),
    )


async def _browse(request: Request, params: BrowseYouTubeParams) -> ToolEnvelope:
    """Browse ingested videos, optionally filtered by topic and source."""
    result = await request.app.state.youtube_service.browse(
        topic=params.topic, source=params.source, logger=_tool_logger(request)
    )
    return _ok_videos(result)


async def _search(request: Request, params: SearchYouTubeParams) -> ToolEnvelope:
    """Keyword Search across saved content and transcript text."""
    result = await request.app.state.youtube_service.search(
        params.q, logger=_tool_logger(request)
    )
    return _ok_videos(result)


async def _fetch_transcript(
    request: Request, params: FetchYouTubeTranscriptParams
) -> ToolEnvelope:
    """Fetch and persist a transcript for an ingested video."""
    result = await request.app.state.youtube_service.fetch_transcript(
        params.video_id, logger=_tool_logger(request)
    )
    return _ok_transcript(result)


async def _ignore(request: Request, params: IgnoreYouTubeVideoParams) -> ToolEnvelope:
    """Purge a video from ingestion."""
    video = await request.app.state.youtube_service.ignore(
        params.video_id, logger=_tool_logger(request)
    )
    return _ok_video(video)


async def _retry(request: Request, params: RetryYouTubeVideoParams) -> ToolEnvelope:
    """Return a previously purged video to ingestion."""
    video = await request.app.state.youtube_service.retry(
        params.video_id, logger=_tool_logger(request)
    )
    return _ok_video(video)


def internal_youtube_tool_routes() -> list[Route]:
    """Mount the YouTube ingestion capabilities as `/internal/tools/*` POSTs.

    Returned separately from the public YouTube routes (and the Memory/Bucket
    tools) so they stay absent from the public OpenAPI document and client.
    """
    return [
        ToolRoute(
            "/internal/tools/browse_youtube",
            ToolEndpoint(BrowseYouTubeParams, _browse),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/search_youtube",
            ToolEndpoint(SearchYouTubeParams, _search),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/fetch_youtube_transcript",
            ToolEndpoint(FetchYouTubeTranscriptParams, _fetch_transcript),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/ignore_youtube_video",
            ToolEndpoint(IgnoreYouTubeVideoParams, _ignore),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/retry_youtube_video",
            ToolEndpoint(RetryYouTubeVideoParams, _retry),
            methods=["POST"],
        ),
    ]
