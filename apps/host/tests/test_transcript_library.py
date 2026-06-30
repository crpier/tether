"""Unit tests for the `youtube-transcript-api` fallback provider.

These never import the real library and never touch a socket. The IP-block
classifier and retry-after parser are exercised directly against stand-in
exception shapes (the resilience logic the worker's global pause depends on), and
the provider's outcome mapping is driven through an injected fake fetcher that
returns snippets or raises the library's named errors.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from snektest import assert_eq, assert_is_none, assert_raises, test

from tether.transcript_library import (
    YouTubeTranscriptApiProvider,
    _classify_library_error,
    _is_transcript_ip_block_error,
    _parse_retry_after,
    _parse_snippets,
)
from tether.youtube import (
    TranscriptBlockedError,
    TranscriptExcludedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
)

# --- Stand-in error shapes (named like the real library's, never imported) ----


class RequestBlocked(Exception):
    """Stand-in for the library's IP-block error."""


class IpBlocked(RequestBlocked):
    """Stand-in for the library's `IpBlocked` (a `RequestBlocked` subclass)."""


class TranscriptsDisabled(Exception):
    """Stand-in for captions disabled on the video."""


class NoTranscriptFound(Exception):
    """Stand-in for no transcript in the requested languages."""


class AgeRestricted(Exception):
    """Stand-in for an age-restricted video."""


class YouTubeRequestFailed(Exception):
    """Stand-in for a generic transport failure."""


class FakeResponse:
    """A `requests`-style response carrying only headers."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers: dict[str, str] = headers


class Snippet:
    """A modern library snippet (`.text` / `.start` / `.duration`)."""

    def __init__(self, text: str, start: float) -> None:
        self.text: str = text
        self.start: float = start


# --- IP-block classification ------------------------------------------------


@test()
def request_blocked_is_an_ip_block() -> None:
    """The library's `RequestBlocked` (and `IpBlocked` subclass) are IP blocks."""
    assert_eq(_is_transcript_ip_block_error(RequestBlocked("blocked")), True)
    assert_eq(_is_transcript_ip_block_error(IpBlocked("blocked")), True)


@test()
def rate_limit_message_is_an_ip_block() -> None:
    """A generic error whose message betrays a rate limit is treated as a block."""
    assert_eq(
        _is_transcript_ip_block_error(RuntimeError("HTTP 429 Too Many Requests")), True
    )


@test()
def ordinary_unavailable_is_not_an_ip_block() -> None:
    """A no-transcript error is not an IP block."""
    assert_eq(_is_transcript_ip_block_error(NoTranscriptFound("nope")), False)


# --- Retry-after parsing ----------------------------------------------------


@test()
def retry_after_seconds_header_is_parsed() -> None:
    """A delta-seconds `Retry-After` header parses to that many seconds."""
    error = RequestBlocked("blocked")
    error.response = FakeResponse({"Retry-After": "120"})  # type: ignore[attr-defined]
    assert_eq(_parse_retry_after(error), timedelta(seconds=120))


@test()
def retry_after_is_found_via_nested_http_error() -> None:
    """The header is found when the response hangs off an `http_error` attribute."""

    class Wrapper(Exception):
        def __init__(self) -> None:
            super().__init__("failed")
            self.http_error = type(
                "E", (), {"response": FakeResponse({"Retry-After": "30"})}
            )()

    assert_eq(_parse_retry_after(Wrapper()), timedelta(seconds=30))


@test()
def missing_retry_after_is_none() -> None:
    """No response / no header yields no hint."""
    assert_is_none(_parse_retry_after(RequestBlocked("blocked")))
    with_resp = RequestBlocked("blocked")
    with_resp.response = FakeResponse({})  # type: ignore[attr-defined]
    assert_is_none(_parse_retry_after(with_resp))


# --- Error -> typed outcome mapping -----------------------------------------


@test()
def classify_maps_request_blocked_to_blocked_with_retry_after() -> None:
    """An IP block maps to the blocked outcome carrying its retry-after hint."""
    blocked = RequestBlocked("blocked")
    blocked.response = FakeResponse({"Retry-After": "300"})  # type: ignore[attr-defined]
    classified = _classify_library_error("v1", blocked)
    assert isinstance(classified, TranscriptBlockedError)
    assert_eq(classified.retry_after, timedelta(seconds=300))


@test()
def classify_maps_transcripts_disabled_to_unavailable() -> None:
    """Captions-disabled maps to the unavailable outcome."""
    classified = _classify_library_error("v1", TranscriptsDisabled("x"))
    assert_eq(isinstance(classified, TranscriptUnavailableError), True)


@test()
def classify_maps_no_transcript_found_to_unavailable() -> None:
    """No transcript in the requested languages maps to the unavailable outcome."""
    classified = _classify_library_error("v1", NoTranscriptFound("x"))
    assert_eq(isinstance(classified, TranscriptUnavailableError), True)


@test()
def classify_maps_age_restricted_to_excluded() -> None:
    """An age-restricted video maps to the excluded outcome."""
    classified = _classify_library_error("v1", AgeRestricted("x"))
    assert_eq(isinstance(classified, TranscriptExcludedError), True)


@test()
def classify_maps_request_failed_to_transient() -> None:
    """A generic transport failure maps to the transient outcome."""
    classified = _classify_library_error("v1", YouTubeRequestFailed("x"))
    assert_eq(isinstance(classified, TranscriptTransientError), True)


# --- Snippet parsing --------------------------------------------------------


@test()
def parse_snippets_joins_text_and_keeps_timing() -> None:
    """Object and dict snippets both parse; blank/untimed cues are dropped."""
    text, segments = _parse_snippets(
        [
            Snippet("Async IO", 1.0),
            {"text": "multiplexes", "start": 2.5},
            Snippet("   ", 3.0),
        ]
    )
    assert_eq(text, "Async IO multiplexes")
    assert_eq(len(segments), 2)
    assert_eq(segments[0].start_seconds, 1.0)
    assert_eq(segments[1].start_seconds, 2.5)


# --- Provider fetch ---------------------------------------------------------


@test()
async def provider_fetch_returns_transcript_from_snippets() -> None:
    """A successful library fetch yields a tagged transcript."""

    def fetcher(video_id: str) -> Any:
        _ = video_id
        return [Snippet("coroutines", 0.0), Snippet("await", 1.0)]

    provider = YouTubeTranscriptApiProvider(fetcher)
    result = await provider.fetch("v1")
    assert_eq(result.text, "coroutines await")
    assert_eq(result.source, "youtube_transcript_api")


@test()
async def provider_fetch_empty_transcript_is_unavailable() -> None:
    """An empty transcript surfaces as the unavailable outcome."""

    def fetcher(video_id: str) -> Any:
        _ = video_id
        return []

    provider = YouTubeTranscriptApiProvider(fetcher)
    with assert_raises(TranscriptUnavailableError):
        _ = await provider.fetch("v1")


@test()
async def provider_fetch_classifies_block() -> None:
    """A `RequestBlocked` from the library surfaces as the blocked outcome."""

    def fetcher(video_id: str) -> Any:
        _ = video_id
        raise RequestBlocked("ip blocked")

    provider = YouTubeTranscriptApiProvider(fetcher)
    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v1")
