"""OAuth-backed concrete `YouTubeApi` adapter + the `just youtube-auth` bootstrap.

The paginated `YouTubeApi` seam (see `tether.youtube`) is, in production, fed by
this thin I/O adapter over the YouTube Data API v3. A plain API key cannot read a
user's own liked list — that needs OAuth, the liked list is exposed only as a
special playlist, and full metadata is a separate batched call. So this module:

* runs an installed-app OAuth flow once (local browser on an ephemeral port, or a
  no-browser mode that just prints the URL), caches the token as JSON, refreshes
  it automatically on expiry, and refuses a token missing a required scope;
* resolves the authenticated channel's *likes* playlist, pages through it, and
  maps each item's added/published timestamps onto `RawYouTubeVideo`;
* fetches full video metadata in id-batched `videos.list` calls.

The adapter holds **no** caching, budgeting, or paging cadence — all of that lives
in the `YouTubeSyncService`/`YouTubeApiClient`, keeping the network boundary as
dumb (and as faked-in-tests) as possible. The Google client libraries
are imported lazily so the rest of Tether runs without them installed; the import
path raises a clear `GoogleClientUnavailableError` when they are missing.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from tether.youtube import (
    FallbackTranscriptProvider,
    FetchedTranscript,
    LikedPage,
    RawYouTubeVideo,
    TranscriptProvider,
    TranscriptSegment,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeQuotaExceededError,
)

YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
"""Read access to the user's account, including the liked-videos playlist."""

YOUTUBE_CAPTION_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
"""The scope captions.download needs; requested now so adding transcript support
later does not force a second authorization."""

REQUIRED_SCOPES: tuple[str, ...] = (YOUTUBE_READONLY_SCOPE, YOUTUBE_CAPTION_SCOPE)
"""Minimum scopes a stored token must carry, validated up front on load."""

_LIKES_PLAYLIST_ALIAS = "LL"
"""The well-known liked-videos playlist alias, used when channel resolution finds
no explicit likes playlist."""

_MAX_IDS_PER_CALL = 50
"""The YouTube Data API `videos.list` per-call id maximum."""

_T = TypeVar("_T")

_NO_PAUSED_SOURCES: frozenset[str] = frozenset()
"""Empty default for `fetch`'s `paused_sources`, hoisted off the parameter list so
it is not constructed in a default expression (`reportCallInDefaultInitializer`)."""

_GOOGLE_INSTALL_HINT = (
    "Google client libraries are not installed. Install them with "
    "`uv pip install google-api-python-client google-auth-oauthlib` "
    "(or add them to the host dependencies) and re-run."
)


class GoogleClientUnavailableError(Exception):
    """Raised when the lazily-imported Google client libraries are missing."""


class YouTubeAuthError(Exception):
    """Raised when a stored token is absent, missing a scope, or unrecoverable.

    The message instructs the user to delete the token and re-run the auth
    recipe; a half-authorized or revoked token fails loudly here rather than
    mid-sync.
    """


@runtime_checkable
class GoogleCredentials(Protocol):
    """The subset of `google.oauth2.credentials.Credentials` the adapter uses."""

    @property
    def valid(self) -> bool:
        """Whether the token is currently usable (present and not expired)."""
        ...

    @property
    def expired(self) -> bool:
        """Whether the token has passed its expiry."""
        ...

    @property
    def refresh_token(self) -> str | None:
        """The refresh token, if the grant issued one."""
        ...

    @property
    def scopes(self) -> Sequence[str] | None:
        """The scopes the token was granted."""
        ...

    def refresh(self, request: object, /) -> None:
        """Refresh the access token in place, or raise on an unrecoverable grant."""
        ...

    def to_json(self) -> str:
        """Serialize the credentials to the cached-token JSON form."""
        ...


class _ListRequest(Protocol):
    """A built Data API request whose `execute()` performs the blocking call."""

    def execute(self) -> dict[str, Any]:
        """Run the request synchronously and return the decoded JSON body."""
        ...


class _DownloadRequest(Protocol):
    """A built caption-download request whose `execute()` returns the raw track."""

    def execute(self) -> bytes | str:
        """Run the download synchronously and return the encoded caption track."""
        ...


class _ResourceCollection(Protocol):
    """A Data API resource collection (e.g. `playlistItems`) exposing `list`."""

    def list(self, **kwargs: Any) -> _ListRequest:
        """Build a list request for this collection with the given parameters."""
        ...


class _CaptionsCollection(Protocol):
    """The `captions` collection, exposing both `list` and `download`."""

    def list(self, **kwargs: Any) -> _ListRequest:
        """Build a caption-track list request for a video."""
        ...

    def download(self, **kwargs: Any) -> _DownloadRequest:
        """Build a caption-track download request for one track id."""
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

    def captions(self) -> _CaptionsCollection:
        """The `captions` collection (track list + SRT download)."""
        ...


type CredentialsFromInfo = Callable[
    [Mapping[str, object], Sequence[str]], GoogleCredentials
]
"""Builds credentials from cached-token info + the required scopes."""

type RequestFactory = Callable[[], object]
"""Builds the transport request object a credentials refresh needs."""

type DiscoveryBuild = Callable[..., _YouTubeResource]
"""Builds the Data API discovery resource from authorized credentials."""


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Paths + toggles for the OAuth flow and token cache.

    `token_path` and `client_secret_path` default under the data dir at the call
    site; `no_browser` prints the auth URL instead of opening a browser, for
    authorizing on a headless box.
    """

    token_path: Path
    client_secret_path: Path
    scopes: tuple[str, ...] = REQUIRED_SCOPES
    no_browser: bool = False


def _load_module(name: str) -> ModuleType:
    """Import a Google client module lazily, mapping absence to a clear error."""
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise GoogleClientUnavailableError(_GOOGLE_INSTALL_HINT) from error


# The Google client libraries ship no type stubs, so their attributes type as
# `Any`; each cast below pins one to the call signature the adapter relies on.
def _default_credentials_from_info() -> CredentialsFromInfo:
    module = _load_module("google.oauth2.credentials")
    return cast("CredentialsFromInfo", module.Credentials.from_authorized_user_info)


def _default_request_factory() -> RequestFactory:
    module = _load_module("google.auth.transport.requests")
    return cast("RequestFactory", module.Request)


def _default_discovery_build() -> DiscoveryBuild:
    module = _load_module("googleapiclient.discovery")
    return cast("DiscoveryBuild", module.build)


def _require_scopes(credentials: GoogleCredentials, required: Sequence[str]) -> None:
    """Reject a token missing any required scope, before it is used for sync."""
    granted = set(credentials.scopes or ())
    missing = [scope for scope in required if scope not in granted]
    if missing:
        message = (
            f"stored YouTube token is missing required scope(s): "
            f"{', '.join(missing)}. Re-run `just youtube-auth` to re-authorize."
        )
        raise YouTubeAuthError(message)


def load_credentials(
    config: OAuthConfig,
    *,
    credentials_from_info: CredentialsFromInfo | None = None,
    request_factory: RequestFactory | None = None,
) -> GoogleCredentials:
    """Load cached credentials, validate scopes, and refresh on expiry.

    Raises `YouTubeAuthError` when the token is absent, missing a required scope,
    cannot be refreshed (revoked/expired with no usable refresh token), or the
    refresh itself fails — every case actionable by re-running the auth recipe. A
    successful refresh is written back to `token_path` so the next run reuses it.

    The Google-backed builders are injectable so the mechanics test against fakes
    without importing the real libraries or hitting the network.
    """
    if not config.token_path.exists():
        message = (
            f"no cached YouTube token at {config.token_path}; "
            f"run `just youtube-auth` to authorize."
        )
        raise YouTubeAuthError(message)
    # `json.loads` returns `Any`; the cached token is always a JSON object here.
    info = cast(
        "Mapping[str, object]", json.loads(config.token_path.read_text("utf-8"))
    )
    build = credentials_from_info or _default_credentials_from_info()
    credentials = build(info, list(config.scopes))
    _require_scopes(credentials, config.scopes)
    if credentials.valid:
        return credentials
    if credentials.refresh_token is None:
        message = (
            f"cached YouTube token at {config.token_path} is expired and cannot "
            f"refresh. Delete it and run `just youtube-auth` to re-authorize."
        )
        raise YouTubeAuthError(message)
    request = (request_factory or _default_request_factory())()
    try:
        credentials.refresh(request)
    except Exception as error:
        message = (
            f"cached YouTube token at {config.token_path} could not be refreshed "
            f"(revoked or unrecoverable). Delete it and run `just youtube-auth` "
            f"to re-authorize."
        )
        raise YouTubeAuthError(message) from error
    _ = config.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


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
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # pyright: ignore[reportArgumentType]  (value is Any from JSON)
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
            return {}
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
            video_id=_str_or_none(resource_id.get("videoId")) or "",
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
            video_id=_str_or_none(item.get("id")) or "",
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


_CAPTION_SRT_FORMAT = "srt"
"""The download format the captions provider requests and parses."""


def _srt_seconds(timestamp: str) -> float:
    """Parse an SRT `HH:MM:SS,mmm` timestamp into whole+fractional seconds."""
    hours, minutes, seconds = timestamp.strip().replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_srt_transcript(srt: str) -> tuple[str, tuple[TranscriptSegment, ...]]:
    """Parse an SRT caption payload into joined text plus timed segments.

    Blocks are separated by blank lines; each carries an index line, a
    `start --> end` timing line, and one or more text lines. Cues with no parsable
    timing or no text are skipped. The joined text is what keyword Search matches.

    >>> text, segments = parse_srt_transcript(
    ...     "1\\n00:00:01,000 --> 00:00:02,000\\nhello\\n"
    ... )
    >>> text
    'hello'
    >>> segments[0].start_seconds
    1.0
    """
    segments: list[TranscriptSegment] = []
    blocks = [
        block for block in srt.replace("\r\n", "\n").split("\n\n") if block.strip()
    ]
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if "-->" in line), None
        )
        if timing_index is None:
            continue
        start_raw = lines[timing_index].split("-->", 1)[0]
        try:
            start = _srt_seconds(start_raw)
        except ValueError, IndexError:
            continue
        text = " ".join(lines[timing_index + 1 :]).strip()
        if text:
            segments.append(TranscriptSegment(start_seconds=start, text=text))
    joined = " ".join(segment.text for segment in segments)
    return joined, tuple(segments)


def _select_caption_track(items: Sequence[Mapping[str, object]]) -> str | None:
    """Pick the best caption track id: prefer a human (non-ASR) track, else first.

    Returns None when there are no usable tracks, which the provider maps to the
    *unavailable* outcome.
    """
    tracks: list[tuple[str, str]] = []
    for item in items:
        track_id = _str_or_none(item.get("id"))
        if track_id is None:
            continue
        snippet = _section(item, "snippet")
        track_kind = (_str_or_none(snippet.get("trackKind")) or "").lower()
        tracks.append((track_id, track_kind))
    if not tracks:
        return None
    for track_id, track_kind in tracks:
        if track_kind != "asr":
            return track_id
    return tracks[0][0]


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


_HTTP_NOT_FOUND = 404
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


def _classify_caption_error(video_id: str, error: Exception) -> Exception:
    """Map a caption API failure onto a typed `TranscriptProvider` signal.

    A 404 (no such captions) and a 403 are both *unavailable*: the captions Data
    API is owner-only, so it 403s for nearly every liked (third-party) video. That
    is "this provider can't serve it", not a global property of the video — so it
    must fall through to the library/Supadata fallbacks rather than raise the
    *excluded* outcome, which would mark the video terminal and purge it from
    ingestion before any fallback runs. A 401 (expired/invalid credentials) is
    *transient* and retryable; everything else — rate limits, 5xx, transport
    errors — is *transient* too.
    """
    status = _http_status(error)
    if status in (_HTTP_NOT_FOUND, _HTTP_FORBIDDEN):
        return TranscriptUnavailableError(video_id)
    if status == _HTTP_UNAUTHORIZED:
        return TranscriptTransientError(
            f"caption fetch for {video_id} unauthorized (credentials): {error}"
        )
    return TranscriptTransientError(f"caption fetch for {video_id} failed: {error}")


async def _no_charge() -> None:
    """The default no-op charge: a captions provider with no bound daily budget."""


class CaptionsTranscriptProvider(TranscriptProvider):
    """The first `TranscriptProvider`: the OAuth-backed YouTube captions API.

    Lists a video's caption tracks, prefers a human-authored (non-ASR) track over
    an auto-generated one, downloads the chosen track as SRT, and parses it into
    transcript text plus timed segments. No tracks, an empty download, or a 403
    (the owner-only API refusing a third-party video) is the *unavailable* outcome
    so the composite falls through to the fallbacks; a 401 and transport/5xx/rate
    errors are *transient*. Blocking Data API calls run in a worker thread.

    This is the only transcript source that spends the YouTube Data API's daily
    quota (`captions.list` + `captions.download` are billed Data API calls; the
    free `youtube_transcript_api` library scrapes the page and Supadata is a
    separate paid HTTP API, so neither should count against it). It charges the
    budget itself, right before its own live call, via `charge` — a callback
    late-bound by `bind_captions_daily_quota` once the budgeted client exists (the
    provider tree is built from settings first, mirroring how Supadata's spend
    guard is late-bound). Unbound (e.g. in tests), `charge` is a no-op.
    """

    source: str = "youtube_captions"

    def __init__(
        self,
        resource: _YouTubeResource,
        *,
        charge: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._resource: _YouTubeResource = resource
        # Public so the wiring can late-bind the daily-quota charge once the
        # budgeted `YouTubeApiClient` exists; a no-op by default.
        self.charge: Callable[[], Awaitable[None]] = charge or _no_charge

    @classmethod
    def from_config(cls, config: OAuthConfig) -> CaptionsTranscriptProvider:
        """Build the production provider: load credentials, build the client.

        Reuses the same credentials (including the `youtube.force-ssl` caption
        scope validated on load) and discovery build as the liked-list adapter.
        """
        credentials = load_credentials(config)
        build = _default_discovery_build()
        resource = build(
            "youtube", "v3", credentials=credentials, cache_discovery=False
        )
        return cls(resource)

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        """Fetch and parse the best caption track, or raise a typed signal.

        Charges the daily Data API budget first (raising `YouTubeQuotaExceededError`
        before any live call when the day is exhausted); the free library and
        Supadata providers never reach this method, so they never spend it.
        """
        # The captions API is never blockable, so the worker's pause hook is a
        # no-op here; the composite provider is what skips its blockable sources.
        _ = (paused_sources, skip_sources)
        await self.charge()
        items = await asyncio.to_thread(self._list_captions, video_id)
        track_id = _select_caption_track(items)
        if track_id is None:
            raise TranscriptUnavailableError(video_id)
        payload = await asyncio.to_thread(self._download_caption, video_id, track_id)
        text, segments = parse_srt_transcript(payload)
        if not text:
            raise TranscriptUnavailableError(video_id)
        return FetchedTranscript(
            text=text, segments=segments, source="youtube_captions"
        )

    def _list_captions(self, video_id: str) -> list[Mapping[str, object]]:
        try:
            response = (
                self._resource.captions()
                .list(part="snippet", videoId=video_id)
                .execute()
            )
        except Exception as error:
            raise _classify_caption_error(video_id, error) from error
        items = response.get("items", [])
        if not isinstance(items, list):
            return []
        return [
            cast("Mapping[str, object]", item)
            for item in cast("list[object]", items)
            if isinstance(item, dict)
        ]

    def _download_caption(self, video_id: str, track_id: str) -> str:
        try:
            raw = (
                self._resource.captions()
                .download(id=track_id, tfmt=_CAPTION_SRT_FORMAT)
                .execute()
            )
        except Exception as error:
            raise _classify_caption_error(video_id, error) from error
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return raw


def _iter_captions_providers(
    provider: TranscriptProvider,
) -> Iterable[CaptionsTranscriptProvider]:
    """Yield every `CaptionsTranscriptProvider` reachable from `provider`."""
    if isinstance(provider, CaptionsTranscriptProvider):
        yield provider
    elif isinstance(provider, FallbackTranscriptProvider):
        for child in provider.leaf_providers():
            yield from _iter_captions_providers(child)


def bind_captions_daily_quota(
    provider: TranscriptProvider, charge: Callable[[], Awaitable[None]]
) -> None:
    """Late-bind the YouTube Data API daily-quota charge onto every captions leaf.

    Mirrors `bind_supadata_spend_guard`: the provider tree is built from settings
    before the budgeted `YouTubeApiClient` exists, so the charge callback (its
    `charge_transcript`) is attached here at wire time. A no-op when the chain has
    no captions provider — the common case, since the default order
    (`supadata,library`) omits it, and the library/Supadata legs never spend this
    budget at all.
    """
    for leaf in _iter_captions_providers(provider):
        leaf.charge = charge


class _InstalledAppFlow(Protocol):
    """The subset of `InstalledAppFlow` the bootstrap drives."""

    def run_local_server(self, *, port: int, open_browser: bool) -> GoogleCredentials:
        """Run the local-server consent flow and return the granted credentials."""
        ...


class _InstalledAppFlowFactory(Protocol):
    """The `InstalledAppFlow` class, entered via its client-secrets constructor."""

    def from_client_secrets_file(
        self, client_secrets_file: str, scopes: Sequence[str], /
    ) -> _InstalledAppFlow:
        """Build a flow from a downloaded Desktop-app client-secret JSON."""
        ...


def _default_installed_app_flow() -> _InstalledAppFlowFactory:
    module = _load_module("google_auth_oauthlib.flow")
    # The Google libraries ship no type stubs, so the class types as `Any`.
    return cast("_InstalledAppFlowFactory", module.InstalledAppFlow)


@dataclass(frozen=True, slots=True)
class AuthFlowResult:
    """The outcome of a bootstrap run: the cached token path + verified titles."""

    token_path: Path
    recent_titles: list[str]


def run_auth_flow(config: OAuthConfig) -> GoogleCredentials:
    """Run the installed-app OAuth flow once and cache the token to disk.

    Opens the browser on an ephemeral local port, or — in `no_browser` mode —
    prints the authorization URL for a headless box. Requires the OAuth client
    secret JSON to already be in place.
    """
    if not config.client_secret_path.exists():
        message = (
            f"no OAuth client secret at {config.client_secret_path}; download a "
            f"Desktop-app OAuth client JSON from the Google Cloud Console and "
            f"place it there."
        )
        raise YouTubeAuthError(message)
    flow_cls = _default_installed_app_flow()
    flow = flow_cls.from_client_secrets_file(
        str(config.client_secret_path), list(config.scopes)
    )
    credentials = flow.run_local_server(port=0, open_browser=not config.no_browser)
    config.token_path.parent.mkdir(parents=True, exist_ok=True)
    _ = config.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


async def _recent_liked_titles(api: OAuthYouTubeApi, count: int) -> list[str]:
    page = await api.list_liked_page(page_token=None, page_size=count)
    return [video.title for video in page.videos[:count]]


def bootstrap(config: OAuthConfig, *, verify_count: int = 5) -> AuthFlowResult:
    """Authorize, then read the most-recent liked titles as an end-to-end check."""
    _ = run_auth_flow(config)
    api = OAuthYouTubeApi.from_config(config)
    titles = asyncio.run(_recent_liked_titles(api, verify_count))
    return AuthFlowResult(token_path=config.token_path, recent_titles=titles)
