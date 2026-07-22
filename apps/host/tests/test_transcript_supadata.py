"""Unit tests for the Supadata `TranscriptProvider` — offline, never spending.

The Supadata HTTP layer is faked by `FakeSupadataTransport` (scripted `submit` /
`poll` responses), so the provider's submit/poll/extract logic is exercised
against fixture payloads without a network call or an API key. A fake `sleep`
makes the bounded async-job polling resolve instantly. Covered: a direct hit
(string and timed-cue content, tagged with the Supadata source), no-transcript ->
*unavailable*, a 404 -> *unavailable*, a 429 / quota body -> *blocked* with its
retry-after and source, the async job model (pending then complete), a failed
job -> *unavailable*, an over-budget poll -> *transient*, and the transport's
key/`Retry-After` handling. The flag/key gating is asserted against the
`tether.transcript_provider_composition` wiring helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_is_none, assert_raises, assert_true, test

from tether.transcript_supadata import (
    HttpSupadataTransport,
    PersistentSupadataSpendGuard,
    SupadataBudgetExhaustedError,
    SupadataConfig,
    SupadataConfigurationError,
    SupadataResponse,
    SupadataSpendGuard,
    SupadataTranscriptProvider,
    SupadataTransport,
    UnlimitedSupadataSpend,
    _retry_after_seconds,
    _submit_params,
    bind_supadata_spend_guard,
)
from tether.youtube import (
    FallbackTranscriptProvider,
    NullTranscriptProvider,
    SourceUsage,
    TranscriptBlockedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
    create_youtube_schema,
    transcript_provider_usage,
)


class FakeSupadataTransport:
    """A scripted `SupadataTransport`: queued `submit` / `poll` responses.

    Each call pops the next scripted response (the last repeats), so a test can
    script "submit hands back a job, the first poll is pending, the second
    completes" without any HTTP.
    """

    def __init__(
        self,
        *,
        submit: list[SupadataResponse],
        poll: list[SupadataResponse] | None = None,
    ) -> None:
        self._submit: list[SupadataResponse] = list(submit)
        self._poll: list[SupadataResponse] = list(poll or [])
        self.submit_calls: int = 0
        self.poll_calls: int = 0

    async def submit(self, video_id: str) -> SupadataResponse:
        _ = video_id
        self.submit_calls += 1
        return self._submit.pop(0) if len(self._submit) > 1 else self._submit[0]

    async def poll(self, job_id: str) -> SupadataResponse:
        _ = job_id
        self.poll_calls += 1
        return self._poll.pop(0) if len(self._poll) > 1 else self._poll[0]


class RaisingSupadataTransport:
    """A `SupadataTransport` whose `submit`/`poll` raise a raw `httpx2` error.

    Stands in for a real network hiccup (read timeout, connection reset) so
    tests can assert the provider classifies it as *transient* rather than
    letting the transport-layer exception escape unclassified.
    """

    def __init__(self, error: Exception) -> None:
        self._error: Exception = error

    async def submit(self, video_id: str) -> SupadataResponse:
        _ = video_id
        raise self._error

    async def poll(self, job_id: str) -> SupadataResponse:
        _ = job_id
        raise self._error


class SubmitsThenRaisesOnPollTransport:
    """A `SupadataTransport` that hands back a job id, then raises a raw
    `httpx2` error on the follow-up poll — isolates the poll loop's own
    transport-error handling from the submit call's."""

    def __init__(self, job_response: SupadataResponse, error: Exception) -> None:
        self._job_response: SupadataResponse = job_response
        self._error: Exception = error

    async def submit(self, video_id: str) -> SupadataResponse:
        _ = video_id
        return self._job_response

    async def poll(self, job_id: str) -> SupadataResponse:
        _ = job_id
        raise self._error


async def _no_sleep(seconds: float) -> None:
    """A `sleep` stand-in so the bounded poll loop resolves without real waiting."""
    _ = seconds


def _provider(
    transport: SupadataTransport, *, max_poll_attempts: int = 5
) -> SupadataTranscriptProvider:
    config = SupadataConfig(
        poll_interval=timedelta(seconds=0), max_poll_attempts=max_poll_attempts
    )
    return SupadataTranscriptProvider(transport, config=config, sleep=_no_sleep)


class _FakeClock:
    """A controllable monotonic clock whose `sleep` advances it and records waits.

    Lets the request-pacing tests assert the exact delay the throttle inserts
    without waiting real seconds; sleeping moves the clock forward so a subsequent
    `monotonic()` reads the time as if the wait had happened.
    """

    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _paced_provider(
    transport: FakeSupadataTransport, clock: _FakeClock, *, interval_seconds: float
) -> SupadataTranscriptProvider:
    config = SupadataConfig(
        min_request_interval=timedelta(seconds=interval_seconds),
        poll_interval=timedelta(seconds=0),
    )
    return SupadataTranscriptProvider(
        transport, config=config, sleep=clock.sleep, monotonic=clock.monotonic
    )


@test()
async def direct_hit_with_timed_cues_returns_segments_tagged_supadata() -> None:
    """A 200 with timed `content` cues yields joined text + segments tagged supadata."""
    payload = {
        "content": [
            {"text": "hello", "offset": 0},
            {"text": "world", "offset": 1500},
            {"text": "   ", "offset": 2000},
        ]
    }
    transport = FakeSupadataTransport(submit=[SupadataResponse(200, payload)])

    result = await _provider(transport).fetch("v1")

    assert_eq(result.text, "hello world")
    assert_eq(result.source, "supadata")
    assert_eq(len(result.segments), 2)
    assert_eq(result.segments[1].start_seconds, 1.5)
    # A direct hit needs no polling.
    assert_eq(transport.poll_calls, 0)


@test()
async def direct_hit_with_string_content_returns_text() -> None:
    """A 200 with plain-string `content` yields the text and no segments."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(200, {"content": "a plain transcript"})]
    )

    result = await _provider(transport).fetch("v1")

    assert_eq(result.text, "a plain transcript")
    assert_eq(result.segments, ())


@test()
async def empty_content_is_unavailable() -> None:
    """A 200 carrying no usable content maps to *unavailable*."""
    transport = FakeSupadataTransport(submit=[SupadataResponse(200, {"content": ""})])

    with assert_raises(TranscriptUnavailableError):
        _ = await _provider(transport).fetch("v1")


@test()
async def not_found_is_unavailable() -> None:
    """A 404 maps to *unavailable* (Supadata has no transcript for the video)."""
    transport = FakeSupadataTransport(submit=[SupadataResponse(404, {})])

    with assert_raises(TranscriptUnavailableError):
        _ = await _provider(transport).fetch("v1")


@test()
async def rate_limit_is_blocked_with_retry_after_and_source() -> None:
    """A 429 maps to *blocked*, carrying its retry-after hint and the supadata source."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(429, {}, retry_after=timedelta(minutes=5))]
    )

    with assert_raises(TranscriptBlockedError) as caught:
        _ = await _provider(transport).fetch("v1")
    assert_eq(caught.exception.source, "supadata")
    assert_eq(caught.exception.retry_after, timedelta(minutes=5))


@test()
async def quota_error_body_is_blocked() -> None:
    """A non-429 body whose error names a limit/quota still maps to *blocked*."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(403, {"error": "monthly quota exceeded"})]
    )

    with assert_raises(TranscriptBlockedError):
        _ = await _provider(transport).fetch("v1")


@test()
async def server_error_is_transient() -> None:
    """A 5xx with no rate-limit/unavailable signal maps to *transient* (retryable)."""
    transport = FakeSupadataTransport(submit=[SupadataResponse(500, {})])

    with assert_raises(TranscriptTransientError):
        _ = await _provider(transport).fetch("v1")


@test()
async def a_read_timeout_on_submit_is_transient() -> None:
    """A network-layer error (read timeout, connection reset) on the billed
    submit call maps to *transient*, not a raw `httpx2` exception escaping the
    provider — matching the documented contract ("transport error is
    *transient*"). Regression for issue #240-adjacent: an unclassified
    `httpx2.ReadTimeout` used to propagate straight out of `fetch()`, aborting
    the whole worker pass instead of backing off just this video.
    """
    transport = RaisingSupadataTransport(httpx2.ReadTimeout("timed out"))

    with assert_raises(TranscriptTransientError):
        _ = await _provider(transport).fetch("v1")


@test()
async def a_connection_error_while_polling_is_transient() -> None:
    """A network-layer error mid-poll also maps to *transient*, not a raw
    `httpx2` exception."""
    transport = SubmitsThenRaisesOnPollTransport(
        SupadataResponse(202, {"jobId": "job-1"}),
        httpx2.ConnectError("connection reset"),
    )

    with assert_raises(TranscriptTransientError):
        _ = await _provider(transport).fetch("v1")


@test()
async def async_job_pending_then_complete_resolves() -> None:
    """A submit handing back a job id is polled until complete, then stored."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(202, {"jobId": "job-1"})],
        poll=[
            SupadataResponse(200, {"status": "active"}),
            SupadataResponse(200, {"status": "completed", "content": "done body"}),
        ],
    )

    result = await _provider(transport).fetch("v1")

    assert_eq(result.text, "done body")
    assert_eq(result.source, "supadata")
    assert_eq(transport.poll_calls, 2)


@test()
async def async_job_failed_is_unavailable() -> None:
    """A job that reports `failed` maps to *unavailable*."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(202, {"jobId": "job-1"})],
        poll=[SupadataResponse(200, {"status": "failed", "error": "boom"})],
    )

    with assert_raises(TranscriptUnavailableError):
        _ = await _provider(transport).fetch("v1")


@test()
async def async_job_over_poll_budget_is_transient() -> None:
    """A job still pending after `max_poll_attempts` maps to *transient* (retry later)."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(202, {"jobId": "job-1"})],
        poll=[SupadataResponse(200, {"status": "active"})],
    )

    with assert_raises(TranscriptTransientError):
        _ = await _provider(transport, max_poll_attempts=3).fetch("v1")
    assert_eq(transport.poll_calls, 3)


@test()
async def rate_limit_while_polling_is_blocked() -> None:
    """A 429 returned mid-poll maps to *blocked* with the supadata source."""
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(202, {"jobId": "job-1"})],
        poll=[SupadataResponse(429, {}, retry_after=timedelta(minutes=1))],
    )

    with assert_raises(TranscriptBlockedError) as caught:
        _ = await _provider(transport).fetch("v1")
    assert_eq(caught.exception.source, "supadata")


@test()
def http_transport_requires_an_api_key() -> None:
    """Building the production transport without a key fails loudly (no silent no-op)."""
    with assert_raises(SupadataConfigurationError):
        _ = HttpSupadataTransport("")


@test()
def retry_after_parses_delta_seconds_only() -> None:
    """A numeric `Retry-After` parses; a missing or non-numeric one is None."""
    assert_eq(_retry_after_seconds({"Retry-After": "30"}), timedelta(seconds=30))
    assert_is_none(_retry_after_seconds({}))
    assert_is_none(_retry_after_seconds({"Retry-After": "soon"}))


class _CountingGuard(SupadataSpendGuard):
    """A guard that counts charges and can be scripted to exhaust after N uses."""

    def __init__(self, *, cap: int | None = None) -> None:
        self._cap: int | None = cap
        self.charges: int = 0

    async def charge(self) -> None:
        if self._cap is not None and self.charges >= self._cap:
            raise SupadataBudgetExhaustedError(self.charges, self._cap)
        self.charges += 1

    async def snapshot(self, *, now: datetime) -> SourceUsage | None:
        _ = now
        if self._cap is None:
            return None
        return SourceUsage(
            used=self.charges,
            limit=self._cap,
            remaining=max(0, self._cap - self.charges),
            period="2026-07",
        )


@test()
def native_is_the_default_mode() -> None:
    """The config defaults to `native` — one use per call, never AI `generate`."""
    assert_eq(SupadataConfig().mode, "native")


@test()
def the_mode_rides_on_every_submit_param() -> None:
    """The pinned mode is sent on the submit params so Supadata never auto-generates."""
    assert_eq(
        _submit_params("v1", "native"),
        {"url": "https://www.youtube.com/watch?v=v1", "mode": "native"},
    )


@test()
def the_preferred_language_rides_on_the_submit_param() -> None:
    """The most preferred language is sent as `lang` so Supadata returns that track."""
    assert_eq(
        _submit_params("v1", "native", ("ro", "en")),
        {"url": "https://www.youtube.com/watch?v=v1", "mode": "native", "lang": "ro"},
    )


@test()
def no_language_leaves_the_lang_param_off() -> None:
    """With no configured languages the `lang` param is omitted (Supadata's default)."""
    assert_eq(
        _submit_params("v1", "native", ()),
        {"url": "https://www.youtube.com/watch?v=v1", "mode": "native"},
    )


@test()
async def an_exhausted_use_cap_is_blocked_without_billing_a_call() -> None:
    """At the cap, fetch raises *blocked* (supadata source) and never calls the transport."""
    transport = FakeSupadataTransport(submit=[SupadataResponse(200, {"content": "hi"})])
    provider = _provider(transport)
    provider.spend_guard = _CountingGuard(cap=0)

    with assert_raises(TranscriptBlockedError) as caught:
        _ = await provider.fetch("v1")
    assert_eq(caught.exception.source, "supadata")
    assert_eq(transport.submit_calls, 0)


@test()
async def a_use_is_reserved_before_the_billed_call() -> None:
    """A healthy fetch charges the guard once before reaching the transport."""
    transport = FakeSupadataTransport(submit=[SupadataResponse(200, {"content": "hi"})])
    provider = _provider(transport)
    guard = _CountingGuard()
    provider.spend_guard = guard

    result = await provider.fetch("v1")
    assert_eq(result.source, "supadata")
    assert_eq(guard.charges, 1)
    assert_eq(transport.submit_calls, 1)


@test()
async def the_persisted_cap_allows_exactly_max_uses_charges() -> None:
    """The DB-backed guard permits `max_uses` charges, then raises on the next."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    guard = PersistentSupadataSpendGuard(db, max_uses=2)

    await guard.charge()
    await guard.charge()
    with assert_raises(SupadataBudgetExhaustedError):
        await guard.charge()


@test()
async def the_persisted_cap_survives_a_restart() -> None:
    """A fresh guard reads the persisted count, so the cap holds across a restart."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    await PersistentSupadataSpendGuard(db, max_uses=1).charge()

    reborn = PersistentSupadataSpendGuard(db, max_uses=1)
    with assert_raises(SupadataBudgetExhaustedError):
        await reborn.charge()


class FakeClock:
    """A controllable clock so monthly-cap tests can cross a month boundary."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@test()
async def the_monthly_cap_resets_at_the_next_utc_month() -> None:
    """An exhausted month's cap starts fresh once the clock rolls into a new month."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    guard = PersistentSupadataSpendGuard(db, max_uses=1, clock=clock)
    await guard.charge()

    clock.advance(timedelta(days=20))  # into August
    await guard.charge()  # the new month's budget is fresh; does not raise


@test()
async def an_exhausted_cap_reports_the_wait_until_the_month_boundary() -> None:
    """The exhausted-cap error carries the time until the monthly budget resets."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 7, 31, 23, 0, tzinfo=UTC))
    guard = PersistentSupadataSpendGuard(db, max_uses=1, clock=clock)
    await guard.charge()

    with assert_raises(SupadataBudgetExhaustedError) as raised:
        await guard.charge()

    # One hour remains until 2026-08-01T00:00Z, when the cap resets.
    assert_eq(raised.exception.retry_after, timedelta(hours=1))


@test()
async def binding_the_cap_reaches_supadata_inside_a_fallback_chain() -> None:
    """`bind_supadata_spend_guard` walks a composite to bind the Supadata leaf."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    supadata = _provider(FakeSupadataTransport(submit=[SupadataResponse(200, {})]))
    chain = FallbackTranscriptProvider(supadata, fallbacks=[NullTranscriptProvider()])

    bind_supadata_spend_guard(chain, db, max_uses=5)

    assert_true(isinstance(supadata.spend_guard, PersistentSupadataSpendGuard))


# --- Monthly usage snapshot (separate from the YouTube daily quota) ---------


@test()
async def guard_snapshot_reports_used_limit_and_month_without_charging() -> None:
    """`snapshot` reads the month's usage but never reserves a use."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    guard = PersistentSupadataSpendGuard(db, max_uses=3000, clock=clock)
    await guard.charge()
    await guard.charge()

    usage = await guard.snapshot(now=clock.now())

    assert_eq(usage.used, 2)
    assert_eq(usage.limit, 3000)
    assert_eq(usage.remaining, 2998)
    assert_eq(usage.period, "2026-07")
    # A snapshot never spends: a further charge still counts from 2, not 3.
    await guard.charge()
    assert_eq((await guard.snapshot(now=clock.now())).used, 3)


@test()
async def guard_snapshot_reports_zero_used_with_no_prior_charge() -> None:
    """A month with no charges yet snapshots as fully unused, not an error."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    guard = PersistentSupadataSpendGuard(db, max_uses=10)

    usage = await guard.snapshot(now=datetime(2026, 7, 1, tzinfo=UTC))

    assert_eq(usage.used, 0)
    assert_eq(usage.remaining, 10)


@test()
async def unlimited_guard_snapshot_is_none() -> None:
    """The unbounded default guard reports no usage (there is no cap)."""
    usage = await UnlimitedSupadataSpend().snapshot(
        now=datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert_is_none(usage)


@test()
async def usage_finds_the_bound_supadata_leaf_inside_a_chain() -> None:
    """`transcript_provider_usage` finds Supadata inside a fallback chain, keyed
    by its `"supadata"` source — the generic replacement for
    `ProviderSupadataUsageReader`."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    supadata = _provider(FakeSupadataTransport(submit=[SupadataResponse(200, {})]))
    chain = FallbackTranscriptProvider(supadata, fallbacks=[NullTranscriptProvider()])
    bind_supadata_spend_guard(chain, db, max_uses=100)

    usage = await transcript_provider_usage(chain, now=datetime(2026, 7, 1, tzinfo=UTC))

    assert "supadata" in usage
    assert_eq(usage["supadata"].used, 0)
    assert_eq(usage["supadata"].limit, 100)


@test()
async def usage_is_empty_with_no_supadata_in_the_chain() -> None:
    """A chain with no Supadata leaf (e.g. captions/library only) reports no usage."""
    usage = await transcript_provider_usage(
        NullTranscriptProvider(), now=datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert_eq(usage, {})


# --- Request pacing: stay under the plan's per-request rate limit ------------


@test()
async def sequential_submits_are_paced_by_the_min_interval() -> None:
    """A configured min interval delays the next submit by the unspent remainder,
    so back-to-back videos don't burst past the plan's request-rate limit."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(200, {"content": "body"})]
    )
    provider = _paced_provider(transport, clock, interval_seconds=2)

    _ = await provider.fetch("v1")
    # The first submit has no predecessor, so it is not delayed.
    assert_eq(clock.sleeps, [])

    # 0.5s of unrelated work elapses before the next video's submit.
    clock.now += 0.5
    _ = await provider.fetch("v2")

    # Only 0.5s of the 2s interval has passed, so it waits the remaining 1.5s.
    assert_eq(clock.sleeps, [1.5])
    assert_eq(transport.submit_calls, 2)


@test()
async def a_gap_longer_than_the_interval_is_not_paced() -> None:
    """When more than the interval already elapsed, the next submit fires at once."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(200, {"content": "body"})]
    )
    provider = _paced_provider(transport, clock, interval_seconds=2)

    _ = await provider.fetch("v1")
    clock.now += 5.0  # already well past the interval
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def pacing_is_off_when_the_interval_is_zero() -> None:
    """The default (zero interval) inserts no delay — behaviour unchanged."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(200, {"content": "body"})]
    )
    provider = _paced_provider(transport, clock, interval_seconds=0)

    _ = await provider.fetch("v1")
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def an_exhausted_budget_incurs_no_pacing_delay() -> None:
    """Pacing wraps only the billed submit: a call blocked at the cap never waits."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(
        submit=[SupadataResponse(200, {"content": "body"})]
    )
    provider = _paced_provider(transport, clock, interval_seconds=2)
    provider.spend_guard = _CountingGuard(cap=0)

    with assert_raises(TranscriptBlockedError):
        _ = await provider.fetch("v1")

    assert_eq(clock.sleeps, [])
    assert_eq(transport.submit_calls, 0)
