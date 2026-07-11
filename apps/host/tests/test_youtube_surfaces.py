"""Dual-surface behaviour tests for YouTube ingestion.

One app, both shells, seeded with an `InMemoryYouTubeApi` so no live YouTube
call is ever made. The REST routes serve full read models with quota + cache in
the body; the `/internal/tools/*` endpoints serve deliberately compact,
context-budgeted rows with quota + cache on the envelope. Both translate
failures through `tether.youtube_capabilities.YOUTUBE_ERRORS`, and ignore/retry
share one capability execute outright.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test

from tests.surfaces import call_tool, login, surface_client
from tether.youtube import (
    _NO_PAUSED_SOURCES,
    FetchedTranscript,
    InMemoryYouTubeApi,
    RawYouTubeVideo,
    TranscriptBlockedError,
    TranscriptProvider,
)


def video(
    video_id: str,
    *,
    title: str = "A Talk",
    channel: str = "PyConf",
    topic: str = "python",
    description: str = "",
) -> RawYouTubeVideo:
    """Build a raw upstream video with sensible defaults."""
    return RawYouTubeVideo(
        video_id=video_id,
        title=title,
        channel=channel,
        topic=topic,
        description=description,
    )


def make_client(
    root: Path,
    api: InMemoryYouTubeApi,
    *,
    quota_limit: int = 1000,
    provider: TranscriptProvider | None = None,
) -> Any:
    """A dual-surface app whose YouTube service is backed by the in-memory API.

    The background transcript sync is disabled so quota spend and cache hits
    stay deterministic; `surface_client` waits for the deferred boot mirror.
    """
    return surface_client(
        root,
        youtube_api=api,
        youtube_daily_quota_limit=quota_limit,
        transcript_provider=provider,
        transcript_sync_enabled=False,
    )


@test()
def get_youtube_browses_with_quota_and_cache_metadata() -> None:
    """`GET /api/youtube` lists synced videos and exposes the day's quota + cache.

    The boot sync mirrors the seeded liked videos into the corpus; the browse
    itself reads local state, so it reports a cache hit and the day's spend.
    """
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube")

    assert_eq(response.status_code, 200)
    body = response.json()
    found = {item["video_id"] for item in body["videos"]}
    assert_in("v1", found)
    assert_in("v2", found)
    # Browse is local: a cache hit, reporting the boot sync's spend (list+detail).
    assert_eq(body["cache"]["hit"], True)
    assert_eq(body["quota"]["used"], 2)


@test()
def get_youtube_filters_by_topic() -> None:
    """The topic query narrows browse to that topic."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", topic="python"), video("v2", topic="rust")]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube", params={"topic": "python"})

    found = {item["video_id"] for item in response.json()["videos"]}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
def get_youtube_search_matches_saved_content() -> None:
    """`GET /api/youtube/search` matches saved title/description."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="Async Python deep dive")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube/search", params={"q": "async"})

    assert_eq(response.status_code, 200)
    assert_in("v1", {item["video_id"] for item in response.json()["videos"]})


@test()
def get_youtube_search_rejects_a_blank_query() -> None:
    """A blank Search query is a 400."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube/search", params={"q": "   "})

    assert_eq(response.status_code, 400)


@test()
def post_transcript_fetches_and_makes_it_searchable() -> None:
    """`POST /api/youtube/{id}/transcript` fetches text and feeds Search."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk")],
        transcripts={"v1": "today we cover coroutines"},
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        _ = client.get("/api/youtube")

        response = client.post("/api/youtube/v1/transcript")
        assert_eq(response.status_code, 200)
        body = response.json()
        assert_eq(body["transcript"], "today we cover coroutines")
        assert_eq(body["video"]["transcript"], "today we cover coroutines")

        found = client.get("/api/youtube/search", params={"q": "coroutines"})

    assert_in("v1", {item["video_id"] for item in found.json()["videos"]})


@test()
def post_transcript_for_unknown_video_is_404() -> None:
    """A transcript fetch for a non-ingested video is a 404."""
    api = InMemoryYouTubeApi(transcripts={"v1": "body"})
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.post("/api/youtube/v1/transcript")

    assert_eq(response.status_code, 404)


@test()
def post_transcript_when_provider_blocked_is_503() -> None:
    """A provider IP-block surfaces as 503 (retry later), not an unhandled 500."""

    class BlockedProvider(TranscriptProvider):
        source: str = "fake"

        async def fetch(
            self,
            video_id: str,
            *,
            paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
            skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        ) -> FetchedTranscript:
            _ = (paused_sources, skip_sources)
            raise TranscriptBlockedError(f"blocked fetching {video_id}")

    api = InMemoryYouTubeApi(liked=[video("v1", title="Talk")])
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, provider=BlockedProvider()) as client,
    ):
        login(client)
        _ = client.get("/api/youtube")
        response = client.post("/api/youtube/v1/transcript")

    assert_eq(response.status_code, 503)


@test()
def post_ignore_unknown_video_is_404() -> None:
    """Ignoring a never-ingested video is a 404."""
    api = InMemoryYouTubeApi()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.post("/api/youtube/nope/ignore")

    assert_eq(response.status_code, 404)


@test()
def get_youtube_status_reports_sync_progress() -> None:
    """`GET /api/youtube/status` summarises ingested videos, quota, and pauses.

    The boot sync mirrors the seeded liked videos; with no transcript provider
    every video is still owed a transcript, so it is pending (not unavailable),
    and nothing is paused.
    """
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube/status")

    assert_eq(response.status_code, 200)
    body = response.json()
    assert_eq(body["videos_total"], 2)
    assert_eq(body["transcripts_pending"], 2)
    assert_eq(body["transcripts_done"], 0)
    assert_eq(body["transcripts_unavailable"], 0)
    # The boot sync ran, so last-run is stamped and the day's spend is reported.
    assert body["last_synced_at"] is not None
    assert_eq(body["quota"]["used"], 2)
    assert_eq(body["api_paused_until"], None)
    assert_eq(body["transcript_providers_paused"], [])


@test()
def get_youtube_status_requires_authentication() -> None:
    """The status surface is gated behind the app session like the rest."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        response = client.get("/api/youtube/status")

    assert_eq(response.status_code, 401)


@test()
def get_youtube_requires_authentication() -> None:
    """The browser YouTube surface is gated behind the app session."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        response = client.get("/api/youtube")

    assert_eq(response.status_code, 401)


@test()
def browse_returns_videos_with_quota_and_cache_metadata() -> None:
    """A successful browse conforms to the envelope and exposes quota + cache.

    The boot sync mirrors the seeded liked videos; the browse reads local state,
    so the envelope reports a cache hit and the day's persisted spend.
    """
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "browse_youtube")

    assert_eq(envelope["success"], True)
    found = {item["video_id"] for item in envelope["result"]}
    assert_in("v1", found)
    assert_in("v2", found)
    assert_eq(envelope["cache"]["hit"], True)
    assert_eq(envelope["cache"]["source"], "cache")
    assert_eq(envelope["quota"]["limit"], 1000)
    assert_eq(envelope["quota"]["used"], 2)


@test()
def browse_rows_are_compact_and_omit_the_transcript() -> None:
    """List rows carry only pick fields — never the (context-heavy) transcript.

    Even after a transcript is fetched and stored, browse must not echo it back:
    the model fetches a specific transcript on demand.
    """
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk")],
        transcripts={"v1": "today we cover coroutines"},
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call_tool(client, "fetch_youtube_transcript", video_id="v1")
        envelope = call_tool(client, "browse_youtube")

    row = envelope["result"][0]
    assert_not_in("transcript", row)
    # This video has no description, so the optional field is absent.
    assert_not_in("description", row)
    assert_eq(
        set(row),
        {"video_id", "title", "channel", "topic", "source", "state"},
    )


@test()
def list_rows_carry_a_truncated_description() -> None:
    """A row exposes a truncated description so the list self-disambiguates.

    Near-duplicate titles can be told apart from the list alone, without a
    transcript fetch or a reworded re-search.
    """
    long_description = "word " * 200  # ~1000 chars, well over the preview cap.
    api = InMemoryYouTubeApi(
        liked=[video("v1", description=long_description)],
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "browse_youtube")

    description = envelope["result"][0]["description"]
    assert_eq(description.endswith("…"), True)
    assert_eq(len(description) <= 201, True)


@test()
def list_rows_omit_the_description_when_blank() -> None:
    """An empty description leaves the optional field off the row entirely."""
    api = InMemoryYouTubeApi(liked=[video("v1", description="   ")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "browse_youtube")

    assert_not_in("description", envelope["result"][0])


@test()
def browse_caps_rows_at_the_limit() -> None:
    """A browse returns at most `limit` rows."""
    api = InMemoryYouTubeApi(liked=[video(f"v{n}") for n in range(5)])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "browse_youtube", limit=2)

    assert_eq(len(envelope["result"]), 2)


@test()
def search_caps_rows_at_the_limit() -> None:
    """A keyword search returns at most `limit` rows."""
    api = InMemoryYouTubeApi(
        liked=[video(f"v{n}", title="async python") for n in range(5)]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "search_youtube", q="async", limit=2)

    assert_eq(len(envelope["result"]), 2)


@test()
def exhausting_quota_on_a_transcript_yields_a_quota_exceeded_envelope() -> None:
    """A depleted budget surfaces as a well-formed quota_exceeded envelope.

    The boot sync spends the whole day's budget (list + detail) mirroring the one
    liked video, so the upstream transcript fetch has nothing left and the guard
    refuses it before calling out.
    """
    api = InMemoryYouTubeApi(liked=[video("v1")], transcripts={"v1": "body"})
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, quota_limit=2) as client,
    ):
        envelope = call_tool(client, "fetch_youtube_transcript", video_id="v1")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "quota_exceeded")
    assert_is_none(envelope["result"])


@test()
def ignore_then_retry_round_trips_a_video() -> None:
    """Ignoring purges a video from browse; retry returns it."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call_tool(client, "browse_youtube")

        ignored = call_tool(client, "ignore_youtube_video", video_id="v1")
        assert_eq(ignored["result"]["state"], "ignored")

        after_ignore = call_tool(client, "browse_youtube")
        assert_not_in("v1", {v["video_id"] for v in after_ignore["result"]})

        retried = call_tool(client, "retry_youtube_video", video_id="v1")
        assert_eq(retried["result"]["state"], "active")

        after_retry = call_tool(client, "browse_youtube")

    assert_in("v1", {v["video_id"] for v in after_retry["result"]})


@test()
def ignoring_an_unknown_video_yields_a_not_found_envelope() -> None:
    """Purging a never-ingested video is a well-formed not-found envelope."""
    api = InMemoryYouTubeApi()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "ignore_youtube_video", video_id="nope")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "not_found")


@test()
def fetch_transcript_returns_text_and_makes_it_searchable() -> None:
    """Fetching a transcript returns its text and feeds transcript Search."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk")],
        transcripts={"v1": "today we cover coroutines"},
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call_tool(client, "browse_youtube")

        fetched = call_tool(client, "fetch_youtube_transcript", video_id="v1")
        assert_eq(fetched["result"]["transcript"], "today we cover coroutines")
        assert_eq(fetched["cache"]["hit"], False)

        found = call_tool(client, "search_youtube", q="coroutines")

    assert_in("v1", {item["video_id"] for item in found["result"]})


@test()
def search_rejects_a_blank_query() -> None:
    """A blank Search query is a well-formed invalid_input envelope."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call_tool(client, "search_youtube", q="   ")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")
