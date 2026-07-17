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
    LibraryPassBudget,
    YouTubeTranscriptApiProvider,
    _classify_library_error,
    _is_transcript_ip_block_error,
    _parse_retry_after,
    _parse_snippets,
    reset_library_pass_budget,
)
from tether.youtube import (
    FallbackTranscriptProvider,
    NullTranscriptProvider,
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
    # The source must be stamped here (not left for a composite fallback to fill
    # in): when the library runs standalone or as the chain's primary, nothing
    # else ever stamps it, and the worker treats a `None` source as an
    # already-deferred skip rather than a fresh block — so the persisted
    # per-source pause would silently never trip.
    assert_eq(classified.source, "youtube_transcript_api")


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


# --- Per-pass request budget: bound real calls a single sync pass can fire ---


class _CountingFetcher:
    """A fetcher stand-in that always succeeds and counts real invocations."""

    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self, video_id: str) -> Any:
        _ = video_id
        self.calls += 1
        return [Snippet("hi", 0.0)]


@test()
async def unlimited_by_default_matches_prior_behaviour() -> None:
    """With no budget configured (the old constructor shape), nothing is capped."""
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(fetcher)

    for _ in range(10):
        _ = await provider.fetch("v1")

    assert_eq(fetcher.calls, 10)


@test()
async def budget_exhausted_blocks_without_calling_the_fetcher() -> None:
    """Once the per-pass budget is spent, further fetches self-throttle: no
    further real network call, but a typed *blocked* (source-stamped) so the
    worker's normal pause/backoff takes over for the rest of the pass."""
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=2)
    )

    _ = await provider.fetch("v1")
    _ = await provider.fetch("v2")
    with assert_raises(TranscriptBlockedError) as caught:
        _ = await provider.fetch("v3")

    assert_eq(fetcher.calls, 2)
    assert_eq(caught.exception.source, "youtube_transcript_api")


@test()
async def budget_exhaustion_keeps_blocking_the_rest_of_the_pass() -> None:
    """Every fetch past the cap stays blocked (no further real calls) until the
    worker starts a new pass."""
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=1)
    )

    _ = await provider.fetch("v1")
    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v2")
    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v3")

    assert_eq(fetcher.calls, 1)


@test()
async def begin_pass_refills_the_budget() -> None:
    """`begin_pass` (the worker's per-pass reset hook) refills the counter."""
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=1)
    )

    _ = await provider.fetch("v1")
    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v2")

    provider.begin_pass()
    _ = await provider.fetch("v3")

    assert_eq(fetcher.calls, 2)


@test()
async def a_real_block_latches_for_the_rest_of_the_pass() -> None:
    """A genuine IP block also stops further real calls this pass — not just
    budget exhaustion — so a block on request 2 of a budget of 5 doesn't spend
    the remaining 3 hitting an IP that is already blocked."""

    class _OnceBlocked:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, video_id: str) -> Any:
            _ = video_id
            self.calls += 1
            raise RequestBlocked("ip blocked")

    fetcher = _OnceBlocked()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=5)
    )

    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v1")
    with assert_raises(TranscriptBlockedError) as caught:
        _ = await provider.fetch("v2")

    assert_eq(fetcher.calls, 1)
    assert_eq(caught.exception.source, "youtube_transcript_api")


@test()
async def begin_pass_clears_the_block_latch_too() -> None:
    """A new pass gets a fresh chance even after a real block latched the last."""

    class _BlockedOnce:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, video_id: str) -> Any:
            _ = video_id
            self.calls += 1
            if self.calls == 1:
                raise RequestBlocked("ip blocked")
            return [Snippet("hi", 0.0)]

    fetcher = _BlockedOnce()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=5)
    )

    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v1")
    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v2")
    assert_eq(fetcher.calls, 1)

    provider.begin_pass()
    result = await provider.fetch("v3")

    assert_eq(result.text, "hi")
    assert_eq(fetcher.calls, 2)


# --- Request pacing: stay under YouTube's per-request rate tolerance --------


class _FakeClock:
    """A controllable monotonic clock whose `sleep` advances it and records
    waits, mirroring the Supadata provider's pacing test double."""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _paced_provider(
    fetcher: Any, clock: _FakeClock, *, interval_seconds: float
) -> YouTubeTranscriptApiProvider:
    return YouTubeTranscriptApiProvider(
        fetcher,
        budget=LibraryPassBudget(
            min_request_interval=timedelta(seconds=interval_seconds)
        ),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@test()
async def sequential_fetches_are_paced_by_the_min_interval() -> None:
    """A configured min interval delays the next real fetch by the unspent
    remainder, so back-to-back videos don't burst YouTube's rate tolerance."""
    fetcher = _CountingFetcher()
    clock = _FakeClock()
    provider = _paced_provider(fetcher, clock, interval_seconds=5)

    _ = await provider.fetch("v1")
    assert_eq(clock.sleeps, [])  # the first call has no predecessor to pace against

    clock.now += 1.0
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [4.0])
    assert_eq(fetcher.calls, 2)


@test()
async def a_gap_longer_than_the_interval_is_not_paced() -> None:
    """When more than the interval already elapsed, the next fetch fires at once."""
    fetcher = _CountingFetcher()
    clock = _FakeClock()
    provider = _paced_provider(fetcher, clock, interval_seconds=5)

    _ = await provider.fetch("v1")
    clock.now += 10.0
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def pacing_is_off_when_the_interval_is_zero() -> None:
    """The default (zero interval) inserts no delay — behaviour unchanged."""
    fetcher = _CountingFetcher()
    clock = _FakeClock()
    provider = _paced_provider(fetcher, clock, interval_seconds=0)

    _ = await provider.fetch("v1")
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def a_budget_exhausted_call_incurs_no_pacing_delay() -> None:
    """Pacing wraps only the real call: a fetch blocked at the cap never waits."""
    fetcher = _CountingFetcher()
    clock = _FakeClock()
    provider = YouTubeTranscriptApiProvider(
        fetcher,
        budget=LibraryPassBudget(
            max_requests_per_pass=0, min_request_interval=timedelta(seconds=5)
        ),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v1")

    assert_eq(clock.sleeps, [])
    assert_eq(fetcher.calls, 0)


# --- Pass-budget reset wiring: walk a composed chain to find the library ----


@test()
def reset_library_pass_budget_resets_a_nested_provider() -> None:
    """The reset walker finds a `YouTubeTranscriptApiProvider` nested behind a
    `FallbackTranscriptProvider` (mirrors `bind_supadata_spend_guard`'s walk)."""
    fetcher = _CountingFetcher()
    library = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=1)
    )
    chain = FallbackTranscriptProvider(NullTranscriptProvider(), fallbacks=[library])

    library._requests_this_pass = 1

    reset_library_pass_budget(chain)

    assert_eq(library._requests_this_pass, 0)


@test()
def reset_library_pass_budget_is_a_noop_without_a_library_provider() -> None:
    """A chain with no library provider (e.g. Supadata-only) is left alone."""
    chain = FallbackTranscriptProvider(
        NullTranscriptProvider(), fallbacks=[NullTranscriptProvider()]
    )

    reset_library_pass_budget(chain)  # must not raise
