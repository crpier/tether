"""OAuth-backed concrete `YouTubeApi` adapter + the `just youtube-auth` bootstrap.

The paginated `YouTubeApi` seam (see `tether.youtube`) is, in production, fed by
this thin I/O adapter over the YouTube Data API v3. A plain API key cannot read a
user's own liked list — that needs OAuth, the liked list is exposed only as a
special playlist, and full metadata is a separate batched call. So this module:

* resolves the authenticated channel's *likes* playlist, pages through it, and
  maps each item's added/published timestamps onto `RawYouTubeVideo`;
* fetches full video metadata in id-batched `videos.list` calls.

OAuth mechanics (flow, token cache, scope validation) live in the shared
`tether.google_oauth` module; this adapter builds on them and owns only what is
YouTube-specific. The adapter holds **no** caching, budgeting, or paging cadence
— all of that lives in the `YouTubeSyncService`/`YouTubeApiClient`, keeping the
network boundary as dumb (and as faked-in-tests) as possible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from tether.google_oauth import (
    OAuthConfig,
    import_google_module,
    load_credentials,
    run_auth_flow,
)
from tether.youtube.quota import (
    LikedPage,
    RawYouTubeVideo,
    YouTubeQuotaExceededError,
)
from tether.youtube.types import VideoId

YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
"""Read access to the user's account, including the liked-videos playlist."""

REQUIRED_SCOPES: tuple[str, ...] = (YOUTUBE_READONLY_SCOPE,)
"""Minimum scopes a stored token must carry, validated up front on load."""

_LIKES_PLAYLIST_ALIAS = "LL"
"""The well-known liked-videos playlist alias, used when channel resolution finds
no explicit likes playlist."""

_MAX_IDS_PER_CALL = 50
"""The YouTube Data API `videos.list` per-call id maximum."""

_T = TypeVar("_T")


class _ListRequest(Protocol):
    """A built Data API request whose `execute()` performs the blocking call."""

    def execute(self) -> dict[str, Any]:
        """Run the request synchronously and return the decoded JSON body."""
        ...


class _ResourceCollection(Protocol):
    """A Data API resource collection (e.g. `playlistItems`) exposing `list`."""

    def list(self, **kwargs: Any) -> _ListRequest:
        """Build a list request for this collection with the given parameters."""
        ...


class _YouTubeResource(Protocol):
    """The discovery client returned by `googleapiclient.discovery.build`."""

    def channels(self) -> _ResourceCollection:
        """The `channels` collection."""
        ...

    def playlistItems(self) -> _ResourceCollection:  # noqa: N802 (mirrors the Data API method name)
        """The `playlistItems` collection."""
        ...

    def videos(self) -> _ResourceCollection:
        """The `videos` collection."""
        ...


type DiscoveryBuild = Callable[..., _YouTubeResource]
"""Builds the Data API discovery resource from authorized credentials."""


# The Google client libraries ship no type stubs, so their attributes type as
# `Any`; each cast below pins one to the call signature the adapter relies on.
def _default_discovery_build() -> DiscoveryBuild:
    module = import_google_module("googleapiclient.discovery")
    return cast("DiscoveryBuild", module.build)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an RFC3339 Data API timestamp into an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: object) -> int | None:
    """Parse a Data API count (returned as a string) into an int, or None."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _as_bool(value: object) -> bool | None:
    """Coerce a Data API flag (bool or 'true'/'false' string) into a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return None


def _parse_duration_seconds(value: object) -> int | None:
    """Parse an ISO 8601 duration (e.g. `PT1H2M3S`) into whole seconds, or None."""
    if not isinstance(value, str) or not value.startswith("PT"):
        return None
    total = 0
    number = ""
    for char in value[2:]:
        if char.isdigit():
            number += char
            continue
        if not number:
            return None
        amount = int(number)
        if char == "H":
            total += amount * 3600
        elif char == "M":
            total += amount * 60
        elif char == "S":
            total += amount
        else:
            return None
        number = ""
    return total


def _str_or_none(value: object) -> str | None:
    """Return a non-empty string value, else None."""
    return value if isinstance(value, str) and value else None


def _thumbnails(value: object) -> dict[str, str]:
    """Flatten the Data API thumbnails map into {label: url}."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for label, payload in cast("dict[str, object]", value).items():
        if isinstance(payload, dict):
            url = cast("dict[str, object]", payload).get("url")
            if isinstance(url, str):
                out[label] = url
    return out


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return a tuple of the string entries of a Data API list, else empty."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast("list[object]", value) if isinstance(item, str))


def _section(item: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Return a nested object section as a mapping, defaulting to empty."""
    value = item.get(key)
    return cast("Mapping[str, object]", value) if isinstance(value, dict) else {}


class OAuthYouTubeApi:
    """A thin OAuth-backed `YouTubeApi`: liked-page reads + batched metadata.

    Construct it with an already-built discovery resource (tests inject a fake);
    `from_config` is the production path that loads credentials and builds the
    real client. Blocking Data API calls run in a worker thread so the adapter
    satisfies the async seam. It holds no budget or cache — the guarded client
    and the sync own that.
    """

    def __init__(
        self,
        resource: _YouTubeResource,
        *,
        likes_playlist_id: str | None = None,
    ) -> None:
        self._resource: _YouTubeResource = resource
        # Resolved once on first use and cached for the adapter's lifetime, so the
        # channel lookup costs a single extra call rather than one per page.
        self._likes_playlist_id: str | None = likes_playlist_id

    @classmethod
    def from_config(cls, config: OAuthConfig) -> OAuthYouTubeApi:
        """Build the production adapter: load credentials, build the client."""
        credentials = load_credentials(config)
        build = _default_discovery_build()
        resource = build(
            "youtube", "v3", credentials=credentials, cache_discovery=False
        )
        return cls(resource)

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Return one page of the liked-videos playlist and the next-page cursor."""
        playlist_id = await self._resolve_likes_playlist()
        payload = await self._read(
            self._list_playlist_items, playlist_id, page_token, page_size
        )
        items = payload.get("items", [])
        videos = [
            self._map_liked_item(cast("Mapping[str, object]", item))
            for item in items
            if isinstance(item, dict)
        ]
        next_token = payload.get("nextPageToken")
        page_info = _section(payload, "pageInfo")
        upstream_total = page_info.get("totalResults")
        return LikedPage(
            videos=videos,
            next_page_token=next_token if isinstance(next_token, str) else None,
            total_results=upstream_total if isinstance(upstream_total, int) else None,
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Return full metadata for the given ids, batched to the per-call limit.

        Ids the `videos.list` call omits (members-only, private, deleted) are
        simply absent from the result, so the sync skips them.
        """
        ids = list(video_ids)
        if not ids:
            return dict[str, RawYouTubeVideo]()
        # Statistics are volatile, so stamp when this batch read them.
        fetched_at = datetime.now(UTC)
        result: dict[str, RawYouTubeVideo] = {}
        for start in range(0, len(ids), _MAX_IDS_PER_CALL):
            chunk = ids[start : start + _MAX_IDS_PER_CALL]
            payload = await self._read(self._list_videos, chunk)
            for item in payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                raw = self._map_video(cast("Mapping[str, object]", item), fetched_at)
                if raw.video_id:
                    result[raw.video_id] = raw
        return result

    async def _resolve_likes_playlist(self) -> str:
        if self._likes_playlist_id is not None:
            return self._likes_playlist_id
        resolved = await self._read(self._fetch_likes_playlist_id)
        self._likes_playlist_id = resolved
        return resolved

    @staticmethod
    async def _read(func: Callable[..., _T], /, *args: object) -> _T:
        """Run a blocking Data API call off-thread, translating a quota 403.

        A `quotaExceeded` failure on any of the three list calls becomes the
        domain `YouTubeQuotaExceededError` so the sync stops gracefully; every
        other error propagates unchanged to surface loudly.
        """
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as error:
            quota = _as_quota_error(error)
            if quota is not None:
                raise quota from error
            raise

    def _fetch_likes_playlist_id(self) -> str:
        response = (
            self._resource.channels().list(part="contentDetails", mine=True).execute()
        )
        items = response.get("items", [])
        if isinstance(items, list) and items and isinstance(items[0], dict):
            content = _section(cast("Mapping[str, object]", items[0]), "contentDetails")
            related = _section(content, "relatedPlaylists")
            likes = related.get("likes")
            if isinstance(likes, str) and likes:
                return likes
        return _LIKES_PLAYLIST_ALIAS

    def _list_playlist_items(
        self, playlist_id: str, page_token: str | None, page_size: int
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": page_size,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        return self._resource.playlistItems().list(**params).execute()

    def _list_videos(self, video_ids: Sequence[str]) -> dict[str, Any]:
        return (
            self._resource.videos()
            .list(
                part="snippet,contentDetails,statistics,status,topicDetails",
                id=",".join(video_ids),
                maxResults=len(video_ids),
            )
            .execute()
        )

    def _map_liked_item(self, item: Mapping[str, object]) -> RawYouTubeVideo:
        snippet = _section(item, "snippet")
        content = _section(item, "contentDetails")
        resource_id = _section(snippet, "resourceId")
        return RawYouTubeVideo(
            video_id=VideoId(_str_or_none(resource_id.get("videoId")) or ""),
            title=_str_or_none(snippet.get("title")) or "",
            channel=_str_or_none(snippet.get("videoOwnerChannelTitle")) or "",
            channel_id=_str_or_none(snippet.get("videoOwnerChannelId")),
            topic="",
            description=_str_or_none(snippet.get("description")) or "",
            # The playlist item's added timestamp is when the user liked it; the
            # content-details timestamp is when the video itself was published.
            liked_at=_parse_timestamp(snippet.get("publishedAt")),
            video_published_at=_parse_timestamp(content.get("videoPublishedAt")),
        )

    def _map_video(
        self, item: Mapping[str, object], statistics_fetched_at: datetime
    ) -> RawYouTubeVideo:
        snippet = _section(item, "snippet")
        content = _section(item, "contentDetails")
        statistics = _section(item, "statistics")
        status = _section(item, "status")
        topic_details = _section(item, "topicDetails")
        return RawYouTubeVideo(
            video_id=VideoId(_str_or_none(item.get("id")) or ""),
            title=_str_or_none(snippet.get("title")) or "",
            channel=_str_or_none(snippet.get("channelTitle")) or "",
            channel_id=_str_or_none(snippet.get("channelId")),
            topic="",
            description=_str_or_none(snippet.get("description")) or "",
            video_published_at=_parse_timestamp(snippet.get("publishedAt")),
            duration_seconds=_parse_duration_seconds(content.get("duration")),
            category_id=_str_or_none(snippet.get("categoryId")),
            default_language=_str_or_none(snippet.get("defaultLanguage")),
            default_audio_language=_str_or_none(snippet.get("defaultAudioLanguage")),
            caption_available=_as_bool(content.get("caption")),
            privacy_status=_str_or_none(status.get("privacyStatus")),
            licensed_content=_as_bool(content.get("licensedContent")),
            made_for_kids=_as_bool(status.get("madeForKids")),
            live_broadcast_content=_str_or_none(snippet.get("liveBroadcastContent")),
            definition=_str_or_none(content.get("definition")),
            dimension=_str_or_none(content.get("dimension")),
            statistics_view_count=_parse_int(statistics.get("viewCount")),
            statistics_like_count=_parse_int(statistics.get("likeCount")),
            statistics_comment_count=_parse_int(statistics.get("commentCount")),
            statistics_fetched_at=statistics_fetched_at,
            topic_categories=_string_tuple(topic_details.get("topicCategories")),
            tags=_string_tuple(snippet.get("tags")),
            thumbnails=_thumbnails(snippet.get("thumbnails")),
        )


def _http_status(error: Exception) -> int | None:
    """Best-effort HTTP status from a Google client error, across versions."""
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    code = getattr(error, "status_code", None)
    return code if isinstance(code, int) else None


def _as_quota_error(error: Exception) -> YouTubeQuotaExceededError | None:
    """Map Google's `403 quotaExceeded` onto the domain quota signal, else None.

    The local `DailyQuota` guard models Google's budget to pre-empt it, but the
    two diverge (a fresh data volume resets the local counter; the project's real
    budget may be spent by usage elsewhere), so the Data API can still 403 with
    `quotaExceeded`. Surfacing that as the typed signal lets `YouTubeSyncService`
    stop gracefully for the day instead of letting an untranslated `HttpError`
    escape the startup sync and crash the lifespan. The machine `reason` appears
    in the real `HttpError`'s `str()`, which is the cross-version handle here.
    """
    if _http_status(error) == _HTTP_FORBIDDEN and "quotaExceeded" in str(error):
        return YouTubeQuotaExceededError(str(error))
    return None


_HTTP_FORBIDDEN = 403


@dataclass(frozen=True, slots=True)
class AuthFlowResult:
    """The outcome of a bootstrap run: the cached token path + verified titles."""

    token_path: Path
    recent_titles: list[str]


async def _recent_liked_titles(api: OAuthYouTubeApi, count: int) -> list[str]:
    page = await api.list_liked_page(page_token=None, page_size=count)
    return [video.title for video in page.videos[:count]]


def bootstrap(config: OAuthConfig, *, verify_count: int = 5) -> AuthFlowResult:
    """Authorize, then read the most-recent liked titles as an end-to-end check."""
    _ = run_auth_flow(config)
    api = OAuthYouTubeApi.from_config(config)
    titles = asyncio.run(_recent_liked_titles(api, verify_count))
    return AuthFlowResult(token_path=config.token_path, recent_titles=titles)
