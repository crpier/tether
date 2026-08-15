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
from datetime import timedelta

import httpx2
from pydantic import TypeAdapter
from snekok import Err, NonEmptySecretStr, Ok, Result
from snekok.types import NonBlankStr, NonNegativeInt
from snektest import assert_eq, assert_is_none, assert_isinstance, test

from tether.transcripts.supadata import (
    HttpSupadataTransport,
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
    SupadataSubmitResponse,
    SupadataTranscript,
    SupadataTranscriptProvider,
    SupadataTransport,
    SupadataTransportFailure,
    _retry_after_seconds,
    _submit_params,
)
from tether.youtube import (
    TranscriptBlockedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
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
        transport,
        config=config,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
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
