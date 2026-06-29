"""Behaviour tests for the OAuth-backed `YouTubeApi` adapter + token mechanics.

These never import the real Google client libraries, never open a browser, and
never touch a socket. The adapter is exercised against a fake discovery resource
(the channel/playlist/video `list` builders) and asserts the mapped paged videos,
liked-at/publish timestamps, batched-metadata mapping, members-only skipping, and
the parameters sent. The token mechanics (cache, refresh, scope validation,
revocation error) run against a faked credentials layer with a temp token path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anyio import TemporaryDirectory
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_in,
    assert_is_none,
    assert_not_in,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.youtube import (
    DailyQuota,
    TranscriptUnavailableError,
    YouTubeApiClient,
    create_youtube_schema,
)
from tether.youtube_oauth import (
    REQUIRED_SCOPES,
    OAuthConfig,
    OAuthYouTubeApi,
    YouTubeAuthError,
    load_credentials,
)

# --- Fake discovery resource ------------------------------------------------


class FakeListRequest:
    """A built `list` request that returns a canned response on `execute`."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class FakeCollection:
    """A resource collection that records `list` kwargs and pops queued responses."""

    def __init__(self, responses: Sequence[dict[str, Any]]) -> None:
        self._responses: list[dict[str, Any]] = list(responses)
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> FakeListRequest:
        self.calls.append(kwargs)
        return FakeListRequest(self._responses.pop(0))


class FakeResource:
    """A fake YouTube discovery resource over the three collections used."""

    def __init__(
        self,
        *,
        channels: Sequence[dict[str, Any]] = (),
        playlist_items: Sequence[dict[str, Any]] = (),
        videos: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.channels_collection = FakeCollection(channels)
        self.playlist_items_collection = FakeCollection(playlist_items)
        self.videos_collection = FakeCollection(videos)

    def channels(self) -> FakeCollection:
        return self.channels_collection

    def playlistItems(self) -> FakeCollection:
        return self.playlist_items_collection

    def videos(self) -> FakeCollection:
        return self.videos_collection


def channels_response(likes_playlist_id: str | None) -> dict[str, Any]:
    """Build a `channels.list` response carrying (or omitting) a likes playlist."""
    related: dict[str, Any] = {}
    if likes_playlist_id is not None:
        related["likes"] = likes_playlist_id
    return {
        "items": [{"contentDetails": {"relatedPlaylists": related}}],
    }


# --- Adapter: liked-page reads ----------------------------------------------


@test()
async def list_liked_page_maps_items_and_resolves_playlist() -> None:
    """A liked page maps id/title/channel and both timestamps, via the channel."""
    resource = FakeResource(
        channels=[channels_response("LL-mine")],
        playlist_items=[
            {
                "items": [
                    {
                        "snippet": {
                            "title": "Async Python",
                            "description": "a talk",
                            "videoOwnerChannelTitle": "PyConf",
                            "videoOwnerChannelId": "UC-pyconf",
                            "publishedAt": "2024-03-02T10:00:00Z",
                            "resourceId": {"videoId": "v1"},
                        },
                        "contentDetails": {"videoPublishedAt": "2020-01-01T00:00:00Z"},
                    }
                ],
                "nextPageToken": "page-2",
            }
        ],
    )
    api = OAuthYouTubeApi(resource)

    page = await api.list_liked_page(page_token=None, page_size=50)

    assert_eq(page.next_page_token, "page-2")
    assert_eq(len(page.videos), 1)
    video = page.videos[0]
    assert_eq(video.video_id, "v1")
    assert_eq(video.title, "Async Python")
    assert_eq(video.channel, "PyConf")
    assert_eq(video.channel_id, "UC-pyconf")
    assert_eq(video.liked_at, datetime(2024, 3, 2, 10, 0, tzinfo=UTC))
    assert_eq(video.video_published_at, datetime(2020, 1, 1, tzinfo=UTC))
    # The likes playlist resolved from the channel drives the playlist query.
    assert_eq(resource.playlist_items_collection.calls[0]["playlistId"], "LL-mine")


@test()
async def list_liked_page_forwards_cursor_and_page_size() -> None:
    """The sync's cursor + page size are forwarded as pageToken/maxResults."""
    resource = FakeResource(
        channels=[channels_response("LL-mine")],
        playlist_items=[{"items": [], "nextPageToken": None}],
    )
    api = OAuthYouTubeApi(resource)

    page = await api.list_liked_page(page_token="cursor-7", page_size=25)

    assert_is_none(page.next_page_token)
    call = resource.playlist_items_collection.calls[0]
    assert_eq(call["pageToken"], "cursor-7")
    assert_eq(call["maxResults"], 25)


@test()
async def list_liked_page_falls_back_to_well_known_alias() -> None:
    """With no likes playlist on the channel, the well-known `LL` alias is used."""
    resource = FakeResource(
        channels=[channels_response(None)],
        playlist_items=[{"items": [], "nextPageToken": None}],
    )
    api = OAuthYouTubeApi(resource)

    _ = await api.list_liked_page(page_token=None, page_size=50)

    assert_eq(resource.playlist_items_collection.calls[0]["playlistId"], "LL")


@test()
async def likes_playlist_is_resolved_only_once() -> None:
    """The channel lookup is cached, so paging does not re-resolve every call."""
    resource = FakeResource(
        channels=[channels_response("LL-mine")],
        playlist_items=[
            {"items": [], "nextPageToken": "p2"},
            {"items": [], "nextPageToken": None},
        ],
    )
    api = OAuthYouTubeApi(resource)

    _ = await api.list_liked_page(page_token=None, page_size=50)
    _ = await api.list_liked_page(page_token="p2", page_size=50)

    assert_eq(len(resource.channels_collection.calls), 1)


# --- Adapter: batched metadata ----------------------------------------------


@test()
async def fetch_video_metadata_maps_enriched_fields() -> None:
    """A `videos.list` item maps onto the full enriched `RawYouTubeVideo`."""
    resource = FakeResource(
        videos=[
            {
                "items": [
                    {
                        "id": "v1",
                        "snippet": {
                            "title": "Async Python",
                            "description": "deep dive",
                            "channelTitle": "PyConf",
                            "channelId": "UC-pyconf",
                            "publishedAt": "2020-01-01T00:00:00Z",
                            "categoryId": "27",
                            "defaultLanguage": "en",
                            "defaultAudioLanguage": "en-US",
                            "liveBroadcastContent": "none",
                            "tags": ["python", "async"],
                            "thumbnails": {
                                "default": {"url": "http://img/default.jpg"},
                                "high": {"url": "http://img/high.jpg"},
                            },
                        },
                        "contentDetails": {
                            "duration": "PT1H2M3S",
                            "definition": "hd",
                            "dimension": "2d",
                            "caption": "true",
                            "licensedContent": True,
                        },
                        "status": {
                            "privacyStatus": "public",
                            "madeForKids": False,
                        },
                        "statistics": {
                            "viewCount": "1234",
                            "likeCount": "56",
                            "commentCount": "7",
                        },
                        "topicDetails": {
                            "topicCategories": [
                                "https://en.wikipedia.org/wiki/Software"
                            ]
                        },
                    }
                ]
            }
        ],
    )
    api = OAuthYouTubeApi(resource)

    metadata = await api.fetch_video_metadata(["v1"])

    raw = metadata["v1"]
    assert_eq(raw.duration_seconds, 3723)
    assert_eq(raw.category_id, "27")
    assert_eq(raw.default_language, "en")
    assert_eq(raw.default_audio_language, "en-US")
    assert_eq(raw.caption_available, True)
    assert_eq(raw.licensed_content, True)
    assert_eq(raw.made_for_kids, False)
    assert_eq(raw.privacy_status, "public")
    assert_eq(raw.definition, "hd")
    assert_eq(raw.statistics_view_count, 1234)
    assert_eq(raw.statistics_like_count, 56)
    assert_eq(raw.statistics_comment_count, 7)
    assert_eq(raw.tags, ("python", "async"))
    assert_eq(raw.topic_categories, ("https://en.wikipedia.org/wiki/Software",))
    assert_eq(raw.thumbnails["high"], "http://img/high.jpg")


@test()
async def fetch_video_metadata_skips_members_only_omitted_ids() -> None:
    """Ids the `videos.list` call omits (members-only/private) are absent."""
    resource = FakeResource(
        videos=[
            {
                "items": [
                    {"id": "v1", "snippet": {"title": "Public"}},
                ]
            }
        ],
    )
    api = OAuthYouTubeApi(resource)

    metadata = await api.fetch_video_metadata(["v1", "v2-members-only"])

    assert_in("v1", metadata)
    assert_not_in("v2-members-only", metadata)


@test()
async def fetch_video_metadata_batches_to_the_id_limit() -> None:
    """More than 50 ids are chunked across multiple `videos.list` calls."""
    ids = [f"v{n}" for n in range(120)]
    resource = FakeResource(
        videos=[
            {"items": [{"id": vid, "snippet": {"title": vid}} for vid in ids[0:50]]},
            {"items": [{"id": vid, "snippet": {"title": vid}} for vid in ids[50:100]]},
            {"items": [{"id": vid, "snippet": {"title": vid}} for vid in ids[100:120]]},
        ],
    )
    api = OAuthYouTubeApi(resource)

    metadata = await api.fetch_video_metadata(ids)

    assert_eq(len(metadata), 120)
    assert_eq(len(resource.videos_collection.calls), 3)
    assert_eq(len(resource.videos_collection.calls[0]["id"].split(",")), 50)


@test()
async def fetch_video_metadata_empty_makes_no_call() -> None:
    """An empty id list short-circuits without any upstream call."""
    resource = FakeResource()
    api = OAuthYouTubeApi(resource)

    metadata = await api.fetch_video_metadata([])

    assert_eq(metadata, {})
    assert_eq(len(resource.videos_collection.calls), 0)


@test()
async def fetch_transcript_reports_unavailable() -> None:
    """Transcripts are a later slice; the seam method reports absence."""
    api = OAuthYouTubeApi(FakeResource())

    with assert_raises(TranscriptUnavailableError):
        _ = await api.fetch_transcript("v1")


# --- Adapter under the budgeted client --------------------------------------


@fixture
async def budgeted() -> AsyncGenerator[DailyQuota]:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    yield DailyQuota(db, limit=10)
    await db.close()


@test()
async def each_upstream_call_spends_from_the_budget() -> None:
    """Wrapped in the guarded client, a liked-page read spends one unit."""
    quota = await load_fixture(budgeted())
    resource = FakeResource(
        channels=[channels_response("LL-mine")],
        playlist_items=[{"items": [], "nextPageToken": None}],
    )
    client = YouTubeApiClient(OAuthYouTubeApi(resource), quota)

    before = (await client.snapshot()).used
    _ = await client.list_liked_page(page_token=None, page_size=50)
    after = (await client.snapshot()).used

    assert_eq(after - before, 1)


# --- Token mechanics --------------------------------------------------------


class FakeCredentials:
    """A faked `Credentials`: controllable validity/scopes/refresh outcome."""

    def __init__(
        self,
        *,
        valid: bool,
        scopes: Sequence[str],
        refresh_token: str | None = "refresh-token",
        refresh_error: Exception | None = None,
    ) -> None:
        self._valid = valid
        self.scopes: Sequence[str] = scopes
        self.refresh_token = refresh_token
        self.expired = not valid
        self._refresh_error = refresh_error
        self.refresh_calls = 0

    @property
    def valid(self) -> bool:
        return self._valid

    def refresh(self, request: object, /) -> None:
        self.refresh_calls += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        self._valid = True
        self.expired = False

    def to_json(self) -> str:
        return json.dumps({"token": "refreshed", "scopes": list(self.scopes)})


def write_token(directory: str) -> Path:
    """Write a placeholder cached-token file (the fake builder ignores content)."""
    path = Path(directory) / "token.json"
    _ = path.write_text(json.dumps({"token": "stored"}), encoding="utf-8")
    return path


def config_for(token_path: Path) -> OAuthConfig:
    return OAuthConfig(
        token_path=token_path,
        client_secret_path=token_path.parent / "client-secret.json",
    )


@test()
async def load_credentials_errors_when_token_absent() -> None:
    """A missing token raises an actionable auth error, not a low-level one."""
    async with TemporaryDirectory() as tmp:
        config = config_for(Path(tmp) / "token.json")

        with assert_raises(YouTubeAuthError):
            _ = load_credentials(config)


@test()
async def load_credentials_returns_valid_token_without_refresh() -> None:
    """A valid token is returned as-is, with no refresh and no rewrite."""
    async with TemporaryDirectory() as tmp:
        path = write_token(tmp)
        original = path.read_text(encoding="utf-8")
        creds = FakeCredentials(valid=True, scopes=REQUIRED_SCOPES)

        _ = load_credentials(
            config_for(path),
            credentials_from_info=lambda _info, _scopes: creds,
            request_factory=object,
        )

        assert_eq(creds.refresh_calls, 0)
        assert_eq(path.read_text(encoding="utf-8"), original)


@test()
async def load_credentials_refreshes_and_persists_expired_token() -> None:
    """An expired token with a refresh token is refreshed and written back."""
    async with TemporaryDirectory() as tmp:
        path = write_token(tmp)
        creds = FakeCredentials(valid=False, scopes=REQUIRED_SCOPES)

        _ = load_credentials(
            config_for(path),
            credentials_from_info=lambda _info, _scopes: creds,
            request_factory=object,
        )

        assert_eq(creds.refresh_calls, 1)
        # The refreshed JSON is persisted for the next run to reuse.
        assert_in("refreshed", path.read_text(encoding="utf-8"))


@test()
async def load_credentials_errors_when_expired_without_refresh_token() -> None:
    """An expired token lacking a refresh token cannot recover; re-auth needed."""
    async with TemporaryDirectory() as tmp:
        path = write_token(tmp)
        creds = FakeCredentials(valid=False, scopes=REQUIRED_SCOPES, refresh_token=None)

        with assert_raises(YouTubeAuthError):
            _ = load_credentials(
                config_for(path),
                credentials_from_info=lambda _info, _scopes: creds,
                request_factory=object,
            )


@test()
async def load_credentials_maps_refresh_failure_to_auth_error() -> None:
    """A revoked/failed refresh surfaces as the re-authorize auth error."""
    async with TemporaryDirectory() as tmp:
        path = write_token(tmp)
        creds = FakeCredentials(
            valid=False,
            scopes=REQUIRED_SCOPES,
            refresh_error=RuntimeError("invalid_grant"),
        )

        with assert_raises(YouTubeAuthError):
            _ = load_credentials(
                config_for(path),
                credentials_from_info=lambda _info, _scopes: creds,
                request_factory=object,
            )


@test()
async def load_credentials_rejects_token_missing_a_required_scope() -> None:
    """A token missing a required scope fails loudly up front, before sync."""
    async with TemporaryDirectory() as tmp:
        path = write_token(tmp)
        creds = FakeCredentials(valid=True, scopes=(REQUIRED_SCOPES[0],))

        with assert_raises(YouTubeAuthError):
            _ = load_credentials(
                config_for(path),
                credentials_from_info=lambda _info, _scopes: creds,
                request_factory=object,
            )
