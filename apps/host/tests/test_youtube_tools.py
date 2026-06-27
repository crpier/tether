"""Behaviour tests for the loopback internal YouTube ingestion tool surface.

These drive the mounted Starlette app through `TestClient`, calling the
`/internal/tools/*` YouTube endpoints directly — no LLM, no pi, no live YouTube.
The app is wired with a seeded `InMemoryYouTubeApi` so ingestion has data to
mirror. Beyond the shared auth gate and envelope, these assert the
YouTube-specific behaviour: the quota + cache metadata on the envelope, browse
topic filtering, ignore/retry, transcript fetch, and transcript-aware search.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


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
    app = create_app(
        config=AppConfig(
            app_password="test-app-password",
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret="test-session-secret",
            youtube_api=api,
            youtube_quota_limit=quota_limit,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        tool_secret=SECRET,
    )
    cast("SessionRegistry", app.state.session_registry).register(SESSION)
    return TestClient(app)


def call(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke a tool with the known secret and session, returning the envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


@test()
def browse_returns_videos_with_quota_and_cache_metadata() -> None:
    """A successful browse conforms to the envelope and exposes quota + cache."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call(client, "browse_youtube")

    assert_eq(envelope["success"], True)
    found = {item["video_id"] for item in envelope["result"]}
    assert_in("v1", found)
    assert_in("v2", found)
    assert_eq(envelope["cache"]["hit"], False)
    assert_eq(envelope["cache"]["source"], "live")
    assert_eq(envelope["quota"]["limit"], 1000)
    assert_eq(envelope["quota"]["used"], 2)


@test()
def repeated_browse_reports_a_cache_hit() -> None:
    """A second browse is served from cache and the envelope says so."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call(client, "browse_youtube")
        envelope = call(client, "browse_youtube")

    assert_eq(envelope["cache"]["hit"], True)


@test()
def browse_filters_by_topic() -> None:
    """A topic filter narrows browse to that topic."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", topic="python"), video("v2", topic="rust")]
    )
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call(client, "browse_youtube", topic="python")

    found = {item["video_id"] for item in envelope["result"]}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
def browse_exhausting_quota_yields_a_quota_exceeded_envelope() -> None:
    """A depleted budget surfaces as a well-formed quota_exceeded envelope."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, quota_limit=1) as client,
    ):
        envelope = call(client, "browse_youtube")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "quota_exceeded")
    assert_is_none(envelope["result"])


@test()
def ignore_then_retry_round_trips_a_video() -> None:
    """Ignoring purges a video from browse; retry returns it."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call(client, "browse_youtube")

        ignored = call(client, "ignore_youtube_video", video_id="v1")
        assert_eq(ignored["result"]["state"], "ignored")

        after_ignore = call(client, "browse_youtube")
        assert_not_in("v1", {v["video_id"] for v in after_ignore["result"]})

        retried = call(client, "retry_youtube_video", video_id="v1")
        assert_eq(retried["result"]["state"], "active")

        after_retry = call(client, "browse_youtube")

    assert_in("v1", {v["video_id"] for v in after_retry["result"]})


@test()
def ignoring_an_unknown_video_yields_a_not_found_envelope() -> None:
    """Purging a never-ingested video is a well-formed not-found envelope."""
    api = InMemoryYouTubeApi()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call(client, "ignore_youtube_video", video_id="nope")

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
        _ = call(client, "browse_youtube")

        fetched = call(client, "fetch_youtube_transcript", video_id="v1")
        assert_eq(fetched["result"]["transcript"], "today we cover coroutines")
        assert_eq(fetched["cache"]["hit"], False)

        found = call(client, "search_youtube", q="coroutines")

    assert_in("v1", {item["video_id"] for item in found["result"]})


@test()
def search_matches_saved_content() -> None:
    """Search matches against saved title/description even before any fetch."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="Async Python deep dive")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        found = call(client, "search_youtube", q="async")

    assert_in("v1", {item["video_id"] for item in found["result"]})


@test()
def search_rejects_a_blank_query() -> None:
    """A blank Search query is a well-formed invalid_input envelope."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call(client, "search_youtube", q="   ")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")
