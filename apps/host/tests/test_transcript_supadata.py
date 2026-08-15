"""Unit tests for the Supadata `TranscriptProvider` — offline, never spending.

The Supadata HTTP layer is faked by `FakeSupadataTransport` (scripted `submit` /
`poll` responses), so the provider's submit/poll/extract logic is exercised
against fixture payloads without a network call or an API key. A fake `sleep`
makes the bounded async-job polling resolve instantly. Covered: a direct hit
(string and timed-cue content, tagged with the Supadata source), no-transcript ->
*unavailable*, a 403/404 -> *unavailable*, a 429 / quota body -> *blocked* with
its retry-after and source, the async job model (pending then complete), a failed
job -> *unavailable*, an over-budget poll -> *transient*, and the transport's
key/`Retry-After` handling. The flag/key gating is asserted against the
`tether.transcripts.provider_composition` wiring helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx2
from pydantic import TypeAdapter
from snekok import Err, NonEmptySecretStr, Ok, Result
from snekok.types import NonBlankStr, NonNegativeInt
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_is_none, assert_isinstance, test

from tether.transcripts.supadata import (
    HttpSupadataTransport,
    SupadataBudgetExhaustedError,
    SupadataConfig,
    SupadataCue,
    SupadataHttpFailure,
    SupadataJobAccepted,
    SupadataJobCompleted,
    SupadataJobFailed,
    SupadataJobPending,
    SupadataNetworkFailure,
    SupadataPollResponse,
    SupadataProtocolFailure,
    SupadataSpendGuard,
    SupadataSubmitResponse,
    SupadataTranscript,
    SupadataTranscriptProvider,
    SupadataTransport,
    SupadataTransportFailure,
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

type _ScriptedSubmit = SupadataSubmitResponse | SupadataTransportFailure
type _ScriptedPoll = SupadataPollResponse | SupadataTransportFailure


class FakeSupadataTransport:
    """A scripted `SupadataTransport` with queued submit and poll outcomes."""

    def __init__(
        self,
        *,
        submit: Sequence[_ScriptedSubmit],
        poll: Sequence[_ScriptedPoll] | None = None,
    ) -> None:
        self._submit: list[_ScriptedSubmit] = list(submit)
        self._poll: list[_ScriptedPoll] = list(poll or [])
        self.submit_calls: int = 0
        self.poll_calls: int = 0

    async def submit(
        self, video_id: str
    ) -> Result[SupadataSubmitResponse, SupadataTransportFailure]:
        _ = video_id
        self.submit_calls += 1
        outcome = self._submit.pop(0) if len(self._submit) > 1 else self._submit[0]
        if isinstance(
            outcome,
            SupadataHttpFailure | SupadataNetworkFailure | SupadataProtocolFailure,
        ):
            return Err(outcome)
        return Ok(outcome)

    async def poll(
        self, job_id: str
    ) -> Result[SupadataPollResponse, SupadataTransportFailure]:
        _ = job_id
        self.poll_calls += 1
        outcome = self._poll.pop(0) if len(self._poll) > 1 else self._poll[0]
        if isinstance(
            outcome,
            SupadataHttpFailure | SupadataNetworkFailure | SupadataProtocolFailure,
        ):
            return Err(outcome)
        return Ok(outcome)


class FailingSupadataTransport:
    """A `SupadataTransport` whose operations return a scripted failure."""

    def __init__(self, failure: SupadataTransportFailure) -> None:
        self._failure: SupadataTransportFailure = failure

    async def submit(
        self, video_id: str
    ) -> Result[SupadataSubmitResponse, SupadataTransportFailure]:
        _ = video_id
        return Err(self._failure)

    async def poll(
        self, job_id: str
    ) -> Result[SupadataPollResponse, SupadataTransportFailure]:
        _ = job_id
        return Err(self._failure)


class SubmitsThenFailsOnPollTransport:
    """A transport that accepts a job before its poll operation fails."""

    def __init__(
        self, job_response: SupadataJobAccepted, failure: SupadataTransportFailure
    ) -> None:
        self._job_response: SupadataJobAccepted = job_response
        self._failure: SupadataTransportFailure = failure

    async def submit(
        self, video_id: str
    ) -> Result[SupadataSubmitResponse, SupadataTransportFailure]:
        _ = video_id
        return Ok(self._job_response)

    async def poll(
        self, job_id: str
    ) -> Result[SupadataPollResponse, SupadataTransportFailure]:
        _ = job_id
        return Err(self._failure)


async def _no_sleep(seconds: float) -> None:
    """A `sleep` stand-in so the bounded poll loop resolves without real waiting."""
    _ = seconds


def _provider(
    transport: SupadataTransport, *, max_poll_attempts: int = 5
) -> SupadataTranscriptProvider:
    config = SupadataConfig(
        poll_interval=timedelta(seconds=0), max_poll_attempts=max_poll_attempts
    )
    return SupadataTranscriptProvider(
        transport, config=config, sleep=_no_sleep, spend_guard=_CountingGuard()
    )


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
        transport,
        config=config,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        spend_guard=_CountingGuard(),
    )


_API_KEY_ADAPTER: TypeAdapter[NonEmptySecretStr] = TypeAdapter(NonEmptySecretStr)


def _api_key() -> NonEmptySecretStr:
    """Validate the nominal API-key type through its intended Pydantic boundary."""
    return _API_KEY_ADAPTER.validate_python("test-api-key")


@test()
async def http_transport_decodes_a_timed_transcript() -> None:
    """The HTTP boundary returns typed cues for a valid direct response."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            200,
            json={
                "content": [
                    {"duration": 500, "lang": "en", "offset": 0, "text": "hello"}
                ]
            },
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert isinstance(response, Ok)
    assert isinstance(response.value, SupadataTranscript)
    assert_eq(
        response.value.content,
        (
            SupadataCue(
                text=NonBlankStr("hello"),
                offset=NonNegativeInt(0),
                duration=NonNegativeInt(500),
                lang="en",
            ),
        ),
    )


@test()
async def http_transport_decodes_an_accepted_job() -> None:
    """The submit boundary requires Supadata's non-empty `jobId` field."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(202, json={"jobId": "job-1"})

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert_eq(response, Ok(SupadataJobAccepted(job_id="job-1")))


@test()
async def http_transport_decodes_a_completed_job() -> None:
    """The poll boundary returns a typed completed transcript."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            200,
            json={"content": "done body", "status": "completed"},
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.poll("job-1")
    await transport.aclose()

    assert_eq(
        response,
        Ok(SupadataJobCompleted(status="completed", content="done body")),
    )


@test()
async def http_transport_treats_partial_poll_content_as_a_failure() -> None:
    """Supadata's special 206 unavailable response is typed during polling too."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            206,
            json={"error": "transcript-unavailable"},
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.poll("job-1")
    await transport.aclose()

    assert_eq(
        response,
        Err(
            SupadataHttpFailure(
                status_code=206,
                error="transcript-unavailable",
            )
        ),
    )


@test()
async def http_transport_normalizes_an_error_response() -> None:
    """HTTP errors retain only typed classification and cooldown fields."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error": "rate-limit", "message": "slow down"},
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert_eq(
        response,
        Err(
            SupadataHttpFailure(
                status_code=429,
                error="rate-limit",
                retry_after=timedelta(seconds=30),
            )
        ),
    )


@test()
async def http_transport_rejects_a_malformed_success_payload() -> None:
    """Invalid cue types are protocol failures rather than partial transcripts."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            200,
            json={"content": [{"offset": "0", "text": "hello"}]},
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert_eq(response, Err(SupadataProtocolFailure(operation="submit")))


@test()
async def http_transport_rejects_blank_cues() -> None:
    """A blank cue invalidates the payload instead of disappearing silently."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(
            200,
            json={"content": [{"offset": 0, "text": "   "}]},
        )

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert_eq(response, Err(SupadataProtocolFailure(operation="submit")))


@test()
async def http_transport_returns_submit_network_failures() -> None:
    """A submit network fault is returned through the transport failure channel."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.submit("v1")
    await transport.aclose()

    assert_eq(
        response,
        Err(SupadataNetworkFailure(operation="submit", message="timed out")),
    )


@test()
async def http_transport_returns_poll_network_failures() -> None:
    """A poll network fault is returned through the transport failure channel."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection reset", request=request)

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    response = await transport.poll("job-1")
    await transport.aclose()

    assert_eq(
        response,
        Err(SupadataNetworkFailure(operation="poll", message="connection reset")),
    )


@test()
async def malformed_success_payload_is_transient() -> None:
    """Schema drift remains retryable instead of marking a video unavailable."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        _ = request
        return httpx2.Response(200, json={"unexpected": "shape"})

    transport = HttpSupadataTransport(
        _api_key(), http_transport=httpx2.MockTransport(respond)
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptTransientError)
    await transport.aclose()


@test()
async def direct_hit_with_timed_cues_returns_segments_tagged_supadata() -> None:
    """Validated timed cues yield joined text and domain transcript segments."""
    transport = FakeSupadataTransport(
        submit=[
            SupadataTranscript(
                content=(
                    SupadataCue(text=NonBlankStr("hello"), offset=NonNegativeInt(0)),
                    SupadataCue(text=NonBlankStr("world"), offset=NonNegativeInt(1500)),
                )
            )
        ]
    )

    result = (await _provider(transport).fetch("v1")).unwrap()

    assert_eq(result.text, "hello world")
    assert_eq(result.source, "supadata")
    assert_eq(len(result.segments), 2)
    assert_eq(result.segments[1].start_seconds, 1.5)
    assert_eq(transport.poll_calls, 0)


@test()
async def direct_hit_with_string_content_returns_text() -> None:
    """A validated text-only transcript yields text and no segments."""
    transport = FakeSupadataTransport(
        submit=[SupadataTranscript(content="a plain transcript")]
    )

    result = (await _provider(transport).fetch("v1")).unwrap()

    assert_eq(result.text, "a plain transcript")
    assert_eq(result.segments, ())


@test()
async def empty_content_is_unavailable() -> None:
    """A valid transcript carrying no usable content maps to unavailable."""
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="")])

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def not_found_is_unavailable() -> None:
    """A 404 maps to unavailable for this video."""
    transport = FakeSupadataTransport(submit=[SupadataHttpFailure(status_code=404)])

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def partial_content_status_is_unavailable() -> None:
    """Supadata's HTTP 206 means transcript unavailable, not partial success."""
    transport = FakeSupadataTransport(
        submit=[
            SupadataHttpFailure(
                status_code=206,
                error="transcript-unavailable",
            )
        ]
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def forbidden_is_unavailable() -> None:
    """A plain 403 is permanent for the video."""
    transport = FakeSupadataTransport(
        submit=[SupadataHttpFailure(status_code=403, error="forbidden")]
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def rate_limit_is_blocked_with_retry_after_and_source() -> None:
    """A 429 maps to blocked with its cooldown and provider source."""
    transport = FakeSupadataTransport(
        submit=[
            SupadataHttpFailure(
                status_code=429,
                retry_after=timedelta(minutes=5),
            )
        ]
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    error = assert_isinstance(failure.error, TranscriptBlockedError)
    assert_eq(error.source, "supadata")
    assert_eq(error.retry_after, timedelta(minutes=5))


@test()
async def quota_error_body_is_blocked() -> None:
    """An error code naming a quota maps to blocked even without HTTP 429."""
    transport = FakeSupadataTransport(
        submit=[
            SupadataHttpFailure(
                status_code=403,
                error="monthly quota exceeded",
            )
        ]
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptBlockedError)


@test()
async def server_error_is_transient() -> None:
    """An ordinary 5xx maps to a retryable transient failure."""
    transport = FakeSupadataTransport(submit=[SupadataHttpFailure(status_code=500)])

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptTransientError)


@test()
async def a_read_timeout_on_submit_is_transient() -> None:
    """A network fault on submit maps to transient."""
    transport = FailingSupadataTransport(
        SupadataNetworkFailure(operation="submit", message="timed out")
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptTransientError)


@test()
async def a_connection_error_while_polling_is_transient() -> None:
    """A network fault while polling maps to transient."""
    transport = SubmitsThenFailsOnPollTransport(
        SupadataJobAccepted(job_id="job-1"),
        SupadataNetworkFailure(operation="poll", message="connection reset"),
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptTransientError)


@test()
async def async_job_pending_then_complete_resolves() -> None:
    """A validated pending job is polled until its completed transcript arrives."""
    transport = FakeSupadataTransport(
        submit=[SupadataJobAccepted(job_id="job-1")],
        poll=[
            SupadataJobPending(status="active"),
            SupadataJobCompleted(status="completed", content="done body"),
        ],
    )

    result = (await _provider(transport).fetch("v1")).unwrap()

    assert_eq(result.text, "done body")
    assert_eq(result.source, "supadata")
    assert_eq(transport.poll_calls, 2)


@test()
async def async_job_failed_is_unavailable() -> None:
    """A validated failed job maps to unavailable."""
    transport = FakeSupadataTransport(
        submit=[SupadataJobAccepted(job_id="job-1")],
        poll=[SupadataJobFailed(status="failed")],
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def async_job_completed_without_content_is_unavailable() -> None:
    """A completed job without usable content returns unavailable."""
    transport = FakeSupadataTransport(
        submit=[SupadataJobAccepted(job_id="job-1")],
        poll=[SupadataJobCompleted(status="completed", content="")],
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptUnavailableError)


@test()
async def async_job_over_poll_budget_is_transient() -> None:
    """A job still pending after the poll budget maps to transient."""
    transport = FakeSupadataTransport(
        submit=[SupadataJobAccepted(job_id="job-1")],
        poll=[SupadataJobPending(status="active")],
    )

    failure = await _provider(transport, max_poll_attempts=3).fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptTransientError)
    assert_eq(transport.poll_calls, 3)


@test()
async def rate_limit_while_polling_is_blocked() -> None:
    """A 429 returned while polling maps to blocked for Supadata."""
    transport = FakeSupadataTransport(
        submit=[SupadataJobAccepted(job_id="job-1")],
        poll=[
            SupadataHttpFailure(
                status_code=429,
                retry_after=timedelta(minutes=1),
            )
        ],
    )

    failure = await _provider(transport).fetch("v1")

    assert isinstance(failure, Err)
    error = assert_isinstance(failure.error, TranscriptBlockedError)
    assert_eq(error.source, "supadata")


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

    async def charge(self) -> Result[None, SupadataBudgetExhaustedError]:
        if self._cap is not None and self.charges >= self._cap:
            return Err(SupadataBudgetExhaustedError(self.charges, self._cap))
        self.charges += 1
        return Ok(None)

    async def snapshot(self, *, now: datetime) -> SourceUsage | None:  # pyright: ignore[reportIncompatibleMethodOverride]
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
    """At the cap, fetch returns blocked and never calls the transport."""
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="hi")])
    provider = _provider(transport)
    provider.spend_guard = _CountingGuard(cap=0)

    failure = await provider.fetch("v1")

    assert isinstance(failure, Err)
    error = assert_isinstance(failure.error, TranscriptBlockedError)
    assert_eq(error.source, "supadata")
    assert_eq(transport.submit_calls, 0)


@test()
async def a_use_is_reserved_before_the_billed_call() -> None:
    """A healthy fetch charges the guard once before reaching the transport."""
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="hi")])
    provider = _provider(transport)
    guard = _CountingGuard()
    provider.spend_guard = guard

    result = (await provider.fetch("v1")).unwrap()
    assert_eq(result.source, "supadata")
    assert_eq(guard.charges, 1)
    assert_eq(transport.submit_calls, 1)


@test()
async def the_persisted_cap_allows_exactly_max_uses_charges() -> None:
    """The DB-backed guard permits `max_uses` charges, then returns an error."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    guard = SupadataSpendGuard(db, max_uses=2)

    assert_eq(await guard.charge(), Ok(None))
    assert_eq(await guard.charge(), Ok(None))
    failure = await guard.charge()

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, SupadataBudgetExhaustedError)
    await db.close()


@test()
async def the_persisted_cap_survives_a_restart() -> None:
    """A fresh guard reads the persisted count, so the cap holds across a restart."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    assert_eq(await SupadataSpendGuard(db, max_uses=1).charge(), Ok(None))

    reborn = SupadataSpendGuard(db, max_uses=1)
    failure = await reborn.charge()

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, SupadataBudgetExhaustedError)
    await db.close()


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
    guard = SupadataSpendGuard(db, max_uses=1, clock=clock)
    assert_eq(await guard.charge(), Ok(None))

    clock.advance(timedelta(days=20))  # into August
    assert_eq(await guard.charge(), Ok(None))
    await db.close()


@test()
async def an_exhausted_cap_reports_the_wait_until_the_month_boundary() -> None:
    """The exhausted-cap error carries the time until the monthly budget resets."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 7, 31, 23, 0, tzinfo=UTC))
    guard = SupadataSpendGuard(db, max_uses=1, clock=clock)
    assert_eq(await guard.charge(), Ok(None))

    failure = await guard.charge()

    assert isinstance(failure, Err)
    error = assert_isinstance(failure.error, SupadataBudgetExhaustedError)
    # One hour remains until 2026-08-01T00:00Z, when the cap resets.
    assert_eq(error.retry_after, timedelta(hours=1))
    await db.close()


@test()
async def binding_the_cap_reaches_supadata_inside_a_fallback_chain() -> None:
    """`bind_supadata_spend_guard` walks a composite to bind the Supadata leaf."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    supadata = _provider(FakeSupadataTransport(submit=[SupadataTranscript(content="")]))
    chain = FallbackTranscriptProvider(supadata, fallbacks=[NullTranscriptProvider()])

    bind_supadata_spend_guard(chain, db, max_uses=5)
    await db.close()


# --- Monthly usage snapshot (separate from the YouTube daily quota) ---------


@test()
async def guard_snapshot_reports_used_limit_and_month_without_charging() -> None:
    """`snapshot` reads the month's usage but never reserves a use."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    guard = SupadataSpendGuard(db, max_uses=3000, clock=clock)
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
    await db.close()


@test()
async def guard_snapshot_reports_zero_used_with_no_prior_charge() -> None:
    """A month with no charges yet snapshots as fully unused, not an error."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    guard = SupadataSpendGuard(db, max_uses=10)

    usage = await guard.snapshot(now=datetime(2026, 7, 1, tzinfo=UTC))

    assert_eq(usage.used, 0)
    assert_eq(usage.remaining, 10)
    await db.close()


@test()
async def usage_finds_the_bound_supadata_leaf_inside_a_chain() -> None:
    """`transcript_provider_usage` finds Supadata inside a fallback chain, keyed
    by its `"supadata"` source — the generic replacement for
    `ProviderSupadataUsageReader`."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    supadata = _provider(FakeSupadataTransport(submit=[SupadataTranscript(content="")]))
    chain = FallbackTranscriptProvider(supadata, fallbacks=[NullTranscriptProvider()])
    bind_supadata_spend_guard(chain, db, max_uses=100)

    usage = await transcript_provider_usage(chain, now=datetime(2026, 7, 1, tzinfo=UTC))

    assert "supadata" in usage
    assert_eq(usage["supadata"].used, 0)
    assert_eq(usage["supadata"].limit, 100)
    await db.close()


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
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="body")])
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
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="body")])
    provider = _paced_provider(transport, clock, interval_seconds=2)

    _ = await provider.fetch("v1")
    clock.now += 5.0  # already well past the interval
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def pacing_is_off_when_the_interval_is_zero() -> None:
    """The default (zero interval) inserts no delay — behaviour unchanged."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="body")])
    provider = _paced_provider(transport, clock, interval_seconds=0)

    _ = await provider.fetch("v1")
    _ = await provider.fetch("v2")

    assert_eq(clock.sleeps, [])


@test()
async def an_exhausted_budget_incurs_no_pacing_delay() -> None:
    """Pacing wraps only the billed submit: a call blocked at the cap never waits."""
    clock = _FakeClock()
    transport = FakeSupadataTransport(submit=[SupadataTranscript(content="body")])
    provider = _paced_provider(transport, clock, interval_seconds=2)
    provider.spend_guard = _CountingGuard(cap=0)

    failure = await provider.fetch("v1")

    assert isinstance(failure, Err)
    _ = assert_isinstance(failure.error, TranscriptBlockedError)
    assert_eq(clock.sleeps, [])
    assert_eq(transport.submit_calls, 0)
