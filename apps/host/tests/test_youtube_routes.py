"""REST behaviour tests for YouTube ingestion.

These drive the mounted Starlette app through `TestClient` — request parsing,
route wiring, service behaviour, and response serialisation together — with a
seeded `InMemoryYouTubeApi` so no live YouTube call is ever made. The browser
surface is authenticated, so each test logs in first.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, assert_in, assert_not_in, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


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
    root: Path, api: InMemoryYouTubeApi, *, quota_limit: int = 1000
) -> TestClient:
    """A test app whose YouTube service is backed by the given in-memory API."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                youtube_api=api,
                youtube_quota_limit=quota_limit,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


@test()
def get_youtube_browses_with_quota_and_cache_metadata() -> None:
    """`GET /api/youtube` lists ingested videos and exposes quota + cache."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.get("/api/youtube")

    assert_eq(response.status_code, 200)
    body = response.json()
    found = {item["video_id"] for item in body["videos"]}
    assert_in("v1", found)
    assert_in("v2", found)
    assert_eq(body["cache"]["hit"], False)
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
def post_ignore_then_retry_round_trips_a_video() -> None:
    """Ignore purges a video from browse; retry returns it."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        _ = client.get("/api/youtube")

        ignored = client.post("/api/youtube/v1/ignore")
        assert_eq(ignored.status_code, 200)
        assert_eq(ignored.json()["state"], "ignored")

        after_ignore = client.get("/api/youtube")
        assert_not_in("v1", {v["video_id"] for v in after_ignore.json()["videos"]})

        retried = client.post("/api/youtube/v1/retry")
        assert_eq(retried.json()["state"], "active")

        after_retry = client.get("/api/youtube")

    assert_in("v1", {v["video_id"] for v in after_retry.json()["videos"]})


@test()
def post_ignore_unknown_video_is_404() -> None:
    """Ignoring a never-ingested video is a 404."""
    api = InMemoryYouTubeApi()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        login(client)
        response = client.post("/api/youtube/nope/ignore")

    assert_eq(response.status_code, 404)


@test()
def get_youtube_requires_authentication() -> None:
    """The browser YouTube surface is gated behind the app session."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        response = client.get("/api/youtube")

    assert_eq(response.status_code, 401)
