"""Behavior tests for the youtube-transcript-api source adapter."""

from collections.abc import Callable
from datetime import timedelta

from snekok.result import Err
from snektest import assert_eq, assert_isinstance, test

from tether.transcripts.contracts import (
    TranscriptBlockedFailure,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)
from tether.youtube.transcript_library import (
    TranscriptLibraryConfig,
    YouTubeTranscriptApiSource,
)


class RequestBlocked(Exception):
    """Stand-in for the library's block exception."""


class NoTranscriptFound(Exception):
    """Stand-in for permanent transcript absence."""


class YouTubeRequestFailed(Exception):
    """Stand-in for an ordinary upstream request failure."""


class FakeResponse:
    """Requests-style response carrying retry headers."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers: dict[str, str] = headers


class BlockedWithResponse(RequestBlocked):
    """Block carrying provider cooldown guidance."""

    def __init__(self) -> None:
        super().__init__("blocked")
        self.response: FakeResponse = FakeResponse({"Retry-After": "120"})


def _raising_fetcher(error: Exception) -> Callable[[str], list[dict[str, object]]]:
    def fetch(video_id: str) -> list[dict[str, object]]:
        _ = video_id
        raise error

    return fetch


@test()
async def source_joins_usable_snippet_text() -> None:
    """Legacy mapping snippets preserve exact text and timing."""
    provider = YouTubeTranscriptApiSource(
        lambda _video_id: [
            {"text": " hello ", "start": 0.0, "duration": 0.75},
            {"text": "world", "start": 1.0, "duration": 1.25},
            {"text": " ", "start": 2.0},
        ]
    )

    transcript = (await provider.fetch("video")).unwrap()

    assert_eq(transcript.text, "hello world")
    assert_eq(transcript.source, "youtube_transcript_api")
    assert_eq(
        [(segment.start_ms, segment.duration_ms) for segment in transcript.segments],
        [(0, 750), (1000, 1250)],
    )


@test()
async def youtube_source_resolves_its_id_from_a_media_locator() -> None:
    """The YouTube-specific adapter interprets the generic target locator."""
    fetched: list[str] = []

    def fetch(video_id: str) -> list[dict[str, object]]:
        fetched.append(video_id)
        return [{"text": "hello", "start": 0.0, "duration": 1.0}]

    provider = YouTubeTranscriptApiSource(fetch)

    _ = await provider.fetch("https://www.youtube.com/watch?v=video")

    assert_eq(fetched, ["video"])


@test()
async def empty_source_response_is_unavailable() -> None:
    """A successful response without usable text is permanent absence."""
    provider = YouTubeTranscriptApiSource(lambda _video_id: [])

    outcome = await provider.fetch("video")

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptUnavailableFailure)


@test()
async def source_block_preserves_retry_after() -> None:
    """Known IP blocks retain source identity and cooldown guidance."""
    provider = YouTubeTranscriptApiSource(_raising_fetcher(BlockedWithResponse()))

    outcome = await provider.fetch("video")

    failure = assert_isinstance(
        assert_isinstance(outcome, Err).error,
        TranscriptBlockedFailure,
    )
    assert_eq(failure.source, "youtube_transcript_api")
    assert_eq(failure.retry_after, timedelta(seconds=120))


@test()
async def permanent_library_absence_is_unavailable() -> None:
    """Known no-transcript errors permit the next configured source."""
    provider = YouTubeTranscriptApiSource(
        _raising_fetcher(NoTranscriptFound("missing"))
    )

    outcome = await provider.fetch("video")

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptUnavailableFailure)


@test()
async def ordinary_library_failure_is_transient() -> None:
    """Unexpected request failures remain retryable per video."""
    provider = YouTubeTranscriptApiSource(
        _raising_fetcher(YouTubeRequestFailed("network"))
    )

    outcome = await provider.fetch("video")

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptTransientFailure)


@test()
async def source_paces_consecutive_requests() -> None:
    """Pacing is source-local while pass limits remain orchestration policy."""
    monotonic_values = iter([0.0, 0.0, 2.0, 5.0])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = YouTubeTranscriptApiSource(
        lambda _video_id: [{"text": _video_id}],
        config=TranscriptLibraryConfig(min_request_interval=timedelta(seconds=5)),
        monotonic=lambda: next(monotonic_values),
        sleep=sleep,
    )

    _ = await provider.fetch("first")
    _ = await provider.fetch("second")

    assert_eq(sleeps, [5.0])
