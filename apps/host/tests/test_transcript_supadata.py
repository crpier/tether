"""Unit tests for the Supadata `TranscriptProvider` — offline, never spending.

The Supadata HTTP layer is faked by `FakeSupadataTransport` (scripted `submit` /
`poll` responses), so the provider's submit/poll/extract logic is exercised
against fixture payloads without a network call or an API key. A fake `sleep`
makes the bounded async-job polling resolve instantly. Covered: a direct hit
(string and timed-cue content, tagged with the Supadata source), no-transcript ->
*unavailable*, a 404 -> *unavailable*, a 429 / quota body -> *blocked* with its
retry-after and source, the async job model (pending then complete), a failed
job -> *unavailable*, an over-budget poll -> *transient*, and the transport's
key/`Retry-After` handling. The flag/key gating is asserted against the server
wiring helper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    _retry_after_seconds,
    _submit_params,
    bind_supadata_spend_guard,
)
from tether.youtube import (
    FallbackTranscriptProvider,
    NullTranscriptProvider,
    TranscriptBlockedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
    create_youtube_schema,
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

    async def submit(
        self, video_id: str, *, language: str | None = None
    ) -> SupadataResponse:
        _ = (video_id, language)
        self.submit_calls += 1
        return self._submit.pop(0) if len(self._submit) > 1 else self._submit[0]

    async def poll(self, job_id: str) -> SupadataResponse:
        _ = job_id
        self.poll_calls += 1
        return self._poll.pop(0) if len(self._poll) > 1 else self._poll[0]


async def _no_sleep(seconds: float) -> None:
    """A `sleep` stand-in so the bounded poll loop resolves without real waiting."""
    _ = seconds


def _provider(
    transport: FakeSupadataTransport, *, max_poll_attempts: int = 5
) -> SupadataTranscriptProvider:
    config = SupadataConfig(
        poll_interval=timedelta(seconds=0), max_poll_attempts=max_poll_attempts
    )
    return SupadataTranscriptProvider(transport, config=config, sleep=_no_sleep)


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
            raise SupadataBudgetExhaustedError(
                self.charges,
                self._cap,
                reset_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        self.charges += 1


@test()
def native_is_the_default_mode() -> None:
    """The config defaults to `native` — one use per call, never AI `generate`."""
    assert_eq(SupadataConfig().mode, "native")


@test()
def the_mode_rides_on_every_submit_param() -> None:
    """The pinned mode is sent on the submit params so Supadata never auto-generates."""
    assert_eq(
        _submit_params("v1", "native", language="en"),
        {"videoId": "v1", "mode": "native", "lang": "en"},
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
async def the_monthly_cap_resets_at_the_next_utc_month() -> None:
    """Supadata spend is keyed by UTC calendar month, not lifetime."""
    current = datetime(2026, 6, 30, 23, tzinfo=UTC)
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    guard = PersistentSupadataSpendGuard(db, max_uses=1, clock=lambda: current)
    await guard.charge()
    with assert_raises(SupadataBudgetExhaustedError):
        await guard.charge()

    current = datetime(2026, 7, 1, tzinfo=UTC)
    await guard.charge()
    await db.close()


@test()
async def the_persisted_cap_survives_a_restart() -> None:
    """A fresh guard reads the persisted count, so the cap holds across a restart."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    await PersistentSupadataSpendGuard(db, max_uses=1).charge()

    reborn = PersistentSupadataSpendGuard(db, max_uses=1)
    with assert_raises(SupadataBudgetExhaustedError):
        await reborn.charge()


@test()
async def binding_the_cap_reaches_supadata_inside_a_fallback_chain() -> None:
    """`bind_supadata_spend_guard` walks a composite to bind the Supadata leaf."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    supadata = _provider(FakeSupadataTransport(submit=[SupadataResponse(200, {})]))
    chain = FallbackTranscriptProvider(supadata, fallbacks=[NullTranscriptProvider()])

    bind_supadata_spend_guard(chain, db, max_uses=5)

    assert_true(isinstance(supadata.spend_guard, PersistentSupadataSpendGuard))
