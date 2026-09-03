"""Dual-surface behaviour tests for YouTube ingestion.

One app, both shells, seeded with an `InMemoryYouTubeApi` so no live YouTube
call is ever made. The REST routes serve full read models with quota + cache in
the body; the `/internal/tools/*` endpoints serve deliberately compact,
context-budgeted rows with quota + cache on the envelope.
Both translate failures through `tether.youtube.capabilities.YOUTUBE_ERRORS`.
"""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snekok.result import Err
from snektest import assert_eq, assert_in, assert_not_in, test

from tests.surfaces import call_tool, login, surface_client
from tether.transcripts.contracts import (
    TranscriptBlockedFailure,
    TranscriptFetchResult,
    TranscriptSource,
)
from tether.youtube.local import InMemoryYouTubeApi
from tether.youtube.quota import RawYouTubeVideo
from tether.youtube.types import VideoId


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
        video_id=VideoId(video_id),
        title=title,
        channel=channel,
        topic=topic,
        description=description,
    )


def activity_video(
    video_id: str,
    *,
    liked_at: datetime,
    duration_seconds: int | None = None,
) -> RawYouTubeVideo:
    """Build a liked video carrying the activity metadata under test."""
    return video(video_id).model_copy(
        update={"liked_at": liked_at, "duration_seconds": duration_seconds}
    )


def make_client(
    root: Path,
    api: InMemoryYouTubeApi,
    *,
    quota_limit: int = 1000,
    provider: TranscriptSource | None = None,
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
    assert_eq(all("transcript_status" not in item for item in body["videos"]), True)


@test()
def youtube_reads_expose_liked_time_and_video_duration() -> None:
    """REST and tool projections expose metadata already stored by ingestion."""
    liked_at = datetime(2026, 8, 20, 12, 30, tzinfo=UTC)
    api = InMemoryYouTubeApi(
        liked=[activity_video("v1", liked_at=liked_at, duration_seconds=3723)],
        transcripts={"v1": "transcript"},
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        rest_video = client.get("/api/youtube").json()["videos"][0]
        tool_video = call_tool(client, "browse_youtube")["result"][0]

    for projected in (rest_video, tool_video):
        assert_eq(projected["liked_at"], "2026-08-20T12:30:00Z")
        assert_eq(projected["duration_seconds"], 3723)


@test()
def summarize_youtube_activity_rejects_ambiguous_or_empty_ranges() -> None:
    """The aggregate requires aware timestamps and a non-empty forward interval."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        naive = call_tool(
            client,
            "summarize_youtube_activity",
            after="2026-08-10T00:00:00",
            before="2026-08-17T00:00:00",
        )
        empty = call_tool(
            client,
            "summarize_youtube_activity",
            after="2026-08-17T00:00:00Z",
            before="2026-08-17T00:00:00Z",
        )

    assert_eq(naive["success"], False)
    assert_eq(naive["error"]["code"], "invalid_input")
    assert_eq(empty["success"], False)
    assert_eq(empty["error"]["code"], "invalid_input")


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
        assert_not_in("video", body)

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

    class BlockedProvider:
        @property
        def source(self) -> str:
            return "fake"

        async def fetch(self, video_id: str) -> TranscriptFetchResult:
            return Err(
                TranscriptBlockedFailure(
                    message=f"blocked fetching {video_id}",
                    source="fake",
                )
            )

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
def unavailable_transcript_appears_in_the_decision_queue() -> None:
    """A permanent provider failure becomes reviewable with useful video context."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="Captionless talk")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        _ = client.post("/api/youtube/v1/transcript")

        response = client.get("/api/youtube/transcript-decisions")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        [
            {
                "video_id": "v1",
                "title": "Captionless talk",
                "channel": "PyConf",
                "transcript_status": "needs_review",
                "last_error": "https://www.youtube.com/watch?v=v1",
                "attempts": 1,
            }
        ],
    )


@test()
def transcript_decisions_can_keep_trying_or_give_up() -> None:
    """Human decisions either re-open acquisition or settle transcript absence."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        _ = client.post("/api/youtube/v1/transcript")

        keep_trying = client.post("/api/youtube/v1/transcript-decision/keep-trying")
        assert_eq(keep_trying.status_code, 200)
        assert_eq(
            keep_trying.json(),
            {"video_id": "v1", "transcript_status": "pending"},
        )
        assert_eq(client.get("/api/youtube/transcript-decisions").json(), [])

        _ = client.post("/api/youtube/v1/transcript")
        give_up = client.post("/api/youtube/v1/transcript-decision/give-up")
        assert_eq(give_up.status_code, 200)
        assert_eq(
            give_up.json(),
            {"video_id": "v1", "transcript_status": "unavailable"},
        )
        decisions = client.get("/api/youtube/transcript-decisions")

    assert_eq(decisions.json(), [])


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
    assert_eq(body["transcriptions"]["pending"], 2)
    assert_eq(body["transcriptions"]["done"], 0)
    assert_eq(body["transcriptions"]["needs_review"], 0)
    assert_eq(body["transcriptions"]["unavailable"], 0)
    # The boot sync ran, so last-run is stamped and the day's spend is reported.
    assert body["last_synced_at"] is not None
    assert_eq(body["quota"]["used"], 2)
    assert_eq(body["api_paused_until"], None)
    assert_eq(body["transcriptions"]["providers_paused"], [])


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
    assert_eq(all("transcript_status" not in item for item in envelope["result"]), True)


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
    assert_not_in("transcript_status", row)
    assert_eq(
        set(row),
        {
            "video_id",
            "title",
            "channel",
            "topic",
            "source",
            "state",
            "liked_at",
            "duration_seconds",
        },
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
def transcription_decision_does_not_change_the_youtube_video_shape() -> None:
    """Acquisition state stays on Transcript operations, not video list rows."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        failed = call_tool(client, "fetch_youtube_transcript", video_id="v1")
        assert_eq(failed["success"], False)
        assert_eq(failed["error"]["code"], "transcript_needs_review")
        assert_eq(api.transcript_calls, 1)
        needs_review = call_tool(client, "browse_youtube")["result"][0]
        assert_not_in("transcript_status", needs_review)

        login(client)
        _ = client.post("/api/youtube/v1/transcript-decision/give-up")
        unavailable = call_tool(client, "browse_youtube")["result"][0]
        stopped = call_tool(client, "fetch_youtube_transcript", video_id="v1")

    assert_not_in("transcript_status", unavailable)
    assert_eq(stopped["error"]["code"], "transcript_unavailable")
    assert_eq(api.transcript_calls, 1)


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
        assert_not_in("video", fetched["result"])
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
