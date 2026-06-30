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

from pydantic import BaseModel, PositiveInt
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
    derive_ingest_state,
)
from tether.youtube_routes import YouTubeVideoRead

# Assistant-facing browse/search default page size. Caps how many rows a single
# tool call can pour into the model's context (the corpus can hold thousands).
_DEFAULT_LIST_LIMIT = 50

# How much of a video's description a list row carries. Enough to disambiguate
# similar titles (the corpus has many near-duplicates) without pouring full
# descriptions — which can run to thousands of chars — into the model's context.
_DESCRIPTION_PREVIEW_CHARS = 200


class BrowseYouTubeParams(BaseModel):
    """Params for a topic/source-filtered browse of ingested videos."""

    topic: str | None = None
    source: YouTubeSource | None = None
    limit: PositiveInt = _DEFAULT_LIST_LIMIT


class SearchYouTubeParams(BaseModel):
    """Search saved videos by title, description, and transcript text in one pass.

    Each result row already carries a description (and, for transcript matches, a
    snippet), so a single query is usually enough — prefer reading the rows over
    re-searching with reworded terms."""

    q: str
    limit: PositiveInt = _DEFAULT_LIST_LIMIT


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


def _description_preview(description: str) -> str | None:
    """A truncated description for a list row, or None when there's nothing to show.

    Capped at `_DESCRIPTION_PREVIEW_CHARS` with an ellipsis marker so the model
    can tell near-duplicate titles apart from the list alone."""
    text = description.strip()
    if not text:
        return None
    if len(text) <= _DESCRIPTION_PREVIEW_CHARS:
        return text
    return text[:_DESCRIPTION_PREVIEW_CHARS].rstrip() + "…"


def _compact_video(
    video: IngestedVideo[Fetched], *, snippet: str | None = None
) -> dict[str, object]:
    """A transcript-free list row for the model.

    Browse/search lists are bounded for context, so they carry only what's
    needed to pick a video — never the full transcript (the model calls
    `fetch_youtube_transcript` for a specific `video_id` when it needs the text).
    The truncated `description` makes the list self-disambiguating, so the model
    can pick (or rule out) a video without a transcript fetch or a re-search.
    Semantic search additionally attaches `snippet`: the best-matching transcript
    excerpt, so the model can tell similar candidates apart without a fetch."""
    row: dict[str, object] = {
        "video_id": video.video_id,
        "title": video.title,
        "channel": video.channel,
        "topic": video.topic,
        "source": video.source,
        "state": derive_ingest_state(video),
    }
    description = _description_preview(video.description)
    if description is not None:
        row["description"] = description
    if snippet is not None:
        row["snippet"] = snippet
    return row


def _ok_videos(result: BrowseResult | SearchResult) -> ToolEnvelope:
    """Envelope a compact video collection with the call's quota + cache."""
    snippets = result.snippets if isinstance(result, SearchResult) else {}
    return ToolEnvelope(
        success=True,
        result=[
            _compact_video(video, snippet=snippets.get(video.video_id))
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
        topic=params.topic,
        source=params.source,
        limit=params.limit,
        logger=_tool_logger(request),
    )
    return _ok_videos(result)


async def _search(request: Request, params: SearchYouTubeParams) -> ToolEnvelope:
    """Keyword Search across saved content and transcript text."""
    result = await request.app.state.youtube_service.search(
        params.q, limit=params.limit, logger=_tool_logger(request)
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
