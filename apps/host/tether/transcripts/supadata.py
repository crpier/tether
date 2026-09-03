"""Typed async Supadata transport and transcript-provider adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Annotated, Literal, Protocol

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from snekok.result import Err, Ok, Result
from snekok.types import NonBlankStr, NonEmptySecretStr, NonNegativeInt

from tether.transcripts.contracts import (
    AsyncClosable,
    FetchedTranscript,
    TranscriptBlockedFailure,
    TranscriptFailure,
    TranscriptFetchResult,
    TranscriptSegment,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)

_SOURCE = "supadata"
"""The provenance tag stamped onto Supadata transcripts and its pause-state key."""
_PROVIDER_BLOCK_ERROR_CODES: frozenset[str] = frozenset(
    {"limit-exceeded", "unauthorized", "upgrade-required"}
)
"""Documented Supadata errors that prevent the configured account from serving."""
_UNAVAILABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"forbidden", "not-found", "transcript-unavailable"}
)
"""Documented Supadata errors that permanently describe the source video."""


class _SupadataPayload(BaseModel):
    """Strict, immutable base for trusted Supadata response payloads."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class SupadataCue(_SupadataPayload):
    """One validated timed transcript cue from Supadata."""

    text: NonBlankStr
    offset: NonNegativeInt
    duration: NonNegativeInt
    lang: str | None = None


class SupadataTranscript(_SupadataPayload):
    """A direct Supadata transcript containing text or validated timed cues."""

    content: str | tuple[SupadataCue, ...]


class SupadataJobAccepted(_SupadataPayload):
    """A submitted transcript that Supadata will complete asynchronously."""

    job_id: Annotated[str, Field(alias="jobId", min_length=1)]


class SupadataJobPending(_SupadataPayload):
    """An async transcript job that has not reached a terminal state."""

    status: Literal["queued", "active"]


class SupadataJobCompleted(_SupadataPayload):
    """A completed async job carrying a validated transcript."""

    status: Literal["completed"]
    content: str | tuple[SupadataCue, ...]


class SupadataApiError(_SupadataPayload):
    """Structured error identity returned by the Supadata API."""

    code: NonBlankStr = Field(validation_alias="error")
    details: NonBlankStr | None = None
    message: NonBlankStr | None = None


class SupadataJobFailed(_SupadataPayload):
    """An async transcript job that Supadata could not complete."""

    error: SupadataApiError
    status: Literal["failed"]


SupadataOperation = Literal["poll", "submit"]
"""A Supadata transport operation exposed in typed transport failures."""


@dataclass(frozen=True, slots=True)
class SupadataHttpFailure:
    """A non-success Supadata response with normalized classification data."""

    operation: SupadataOperation
    status_code: int
    error: SupadataApiError | None = None
    retry_after: timedelta | None = None


@dataclass(frozen=True, slots=True)
class SupadataNetworkFailure:
    """A network fault prevented Supadata from returning an HTTP response."""

    operation: SupadataOperation
    message: str


@dataclass(frozen=True, slots=True)
class SupadataProtocolFailure:
    """A successful Supadata response did not satisfy the expected schema."""

    operation: SupadataOperation


type SupadataSubmitResponse = SupadataTranscript | SupadataJobAccepted
type SupadataPollResponse = (
    SupadataJobPending | SupadataJobCompleted | SupadataJobFailed
)
type SupadataTransportFailure = (
    SupadataHttpFailure | SupadataNetworkFailure | SupadataProtocolFailure
)
type SupadataSubmitResult = Result[SupadataSubmitResponse, SupadataTransportFailure]
type SupadataPollResult = Result[SupadataPollResponse, SupadataTransportFailure]
type _SupadataPollingResult = Result[
    FetchedTranscript | None,
    TranscriptFailure,
]


class SupadataTransport(Protocol):
    """The typed Supadata HTTP boundary driven by the transcript provider."""

    async def submit(self, locator: str, /) -> SupadataSubmitResult:
        """Request an immediate transcript or an asynchronous job."""
        ...

    async def poll(self, job_id: str) -> SupadataPollResult:
        """Read the current state of an asynchronous transcript job."""
        ...


_SUBMIT_RESPONSE_ADAPTER: TypeAdapter[SupadataTranscript | SupadataJobAccepted] = (
    TypeAdapter(SupadataTranscript | SupadataJobAccepted)
)
_POLL_RESPONSE_ADAPTER: TypeAdapter[
    SupadataJobPending | SupadataJobCompleted | SupadataJobFailed
] = TypeAdapter(SupadataJobPending | SupadataJobCompleted | SupadataJobFailed)
_ERROR_PAYLOAD_ADAPTER: TypeAdapter[SupadataApiError] = TypeAdapter(SupadataApiError)


@dataclass(frozen=True, slots=True)
class SupadataConfig:
    """Tunables for the Supadata provider's HTTP and async-poll behaviour."""

    base_url: str = "https://api.supadata.ai/v1"
    """Supadata API root the transport issues its requests against."""
    languages: tuple[str, ...] = ()
    """Preferred caption languages, most preferred first (ISO codes). The most
    preferred is sent as the `lang` param on each submit so Supadata returns that
    track when it exists; empty leaves the param off (Supadata picks the default)."""
    timeout: timedelta = timedelta(seconds=30)
    """Per-request HTTP timeout for both submit and poll."""
    poll_interval: timedelta = timedelta(seconds=2)
    """How long to wait between polls of an in-flight async transcript job."""
    max_poll_attempts: int = 10
    """Poll budget for an async job; exhausting it is *transient*, not a hang."""
    min_request_interval: timedelta = timedelta(0)
    """Minimum spacing between consecutive billed submits. The worker fetches videos
    back-to-back, so a low-rate plan returns `429 limit-exceeded` on the burst and
    the source is paused; spacing submits keeps them under that per-request rate.
    Zero (the default) disables pacing — behaviour unchanged — so a plan with a
    generous rate incurs no delay."""


def _is_provider_blocked(response: SupadataHttpFailure) -> bool:
    """Whether a failure prevents Supadata from serving the configured account."""
    if response.status_code in {
        httpx2.codes.UNAUTHORIZED.value,
        httpx2.codes.PAYMENT_REQUIRED.value,
        httpx2.codes.TOO_MANY_REQUESTS.value,
    }:
        return True
    if (
        response.operation == "poll"
        and response.status_code == httpx2.codes.FORBIDDEN.value
    ):
        return True
    if response.error is None:
        return False
    return response.error.code.lower() in _PROVIDER_BLOCK_ERROR_CODES


def _is_unavailable(response: SupadataHttpFailure) -> bool:
    """Whether a failure means Supadata permanently lacks this transcript.

    Submit `403` and `404` responses describe inaccessible source videos. Poll
    `404` instead describes an expired or unknown job and remains retryable.
    Supadata's special `206` is transcript-unavailable for either operation.
    """
    if response.status_code == httpx2.codes.PARTIAL_CONTENT.value:
        return True
    if response.operation == "submit" and response.status_code in {
        httpx2.codes.FORBIDDEN.value,
        httpx2.codes.NOT_FOUND.value,
    }:
        return True
    if response.operation == "poll":
        return False
    if response.error is None:
        return False
    return response.error.code.lower() in _UNAVAILABLE_ERROR_CODES


def _extract_transcript(
    transcript: SupadataTranscript | SupadataJobCompleted,
    *,
    video_id: str,
) -> Result[FetchedTranscript, TranscriptUnavailableFailure]:
    """Preserve usable text and exact provider-reported timing."""
    if isinstance(transcript.content, str):
        cleaned = transcript.content.strip()
        segments: tuple[TranscriptSegment, ...] = ()
    else:
        cleaned = " ".join(cue.text.strip() for cue in transcript.content)
        segments = tuple(
            TranscriptSegment(
                text=cue.text,
                start_ms=cue.offset,
                duration_ms=cue.duration,
            )
            for cue in transcript.content
        )
    if not cleaned:
        return Err(TranscriptUnavailableFailure(locator=video_id))
    return Ok(FetchedTranscript(text=cleaned, source=_SOURCE, segments=segments))


def _unfinished_failure(
    video_id: str, job_id: str, attempts: int
) -> TranscriptTransientFailure:
    """Build the transient failure for a job exceeding its bounded poll budget."""
    return TranscriptTransientFailure(
        f"supadata job {job_id} for {video_id} unfinished after {attempts} polls"
    )


def _classify_failure(
    video_id: str, response: SupadataHttpFailure
) -> (
    TranscriptBlockedFailure | TranscriptUnavailableFailure | TranscriptTransientFailure
):
    """Map a non-success Supadata response onto a typed source failure.

    Account and rate failures are the *blocked* outcome (carrying any retry-after
    hint so the worker's Supadata pause honors it); a forbidden or missing source
    video is *unavailable* (terminal); everything else is *transient*.
    """
    if _is_provider_blocked(response):
        return TranscriptBlockedFailure(
            message=f"supadata blocked while fetching {video_id}",
            retry_after=response.retry_after,
            source=_SOURCE,
        )
    if _is_unavailable(response):
        return TranscriptUnavailableFailure(locator=video_id)
    return TranscriptTransientFailure(
        f"supadata {response.operation} for {video_id} failed (status {response.status_code})"
    )


def _classify_transport_failure(
    video_id: str, failure: SupadataTransportFailure
) -> (
    TranscriptBlockedFailure | TranscriptUnavailableFailure | TranscriptTransientFailure
):
    """Translate a transport failure into the provider's failure vocabulary."""
    if isinstance(failure, SupadataHttpFailure):
        return _classify_failure(video_id, failure)
    if isinstance(failure, SupadataNetworkFailure):
        return TranscriptTransientFailure(
            f"supadata {failure.operation} for {video_id} failed: {failure.message}"
        )
    return TranscriptTransientFailure(
        f"supadata {failure.operation} for {video_id} returned an invalid response"
    )


def _classify_job_failure(
    video_id: str, failure: SupadataJobFailed
) -> (
    TranscriptBlockedFailure | TranscriptUnavailableFailure | TranscriptTransientFailure
):
    """Classify a failed async job by its preserved Supadata error code."""
    error_code = failure.error.code.lower()
    if error_code in _PROVIDER_BLOCK_ERROR_CODES:
        return TranscriptBlockedFailure(
            message=f"supadata account blocked while processing {video_id}",
            source=_SOURCE,
        )
    if error_code in _UNAVAILABLE_ERROR_CODES:
        return TranscriptUnavailableFailure(locator=video_id)
    return TranscriptTransientFailure(
        f"supadata job for {video_id} failed ({failure.error.code})"
    )


class SupadataTranscriptSource:
    """Fetch transcripts through Supadata's direct and asynchronous responses.

    It submits a transcript request, returns immediately on a direct hit, and
    otherwise polls the async job to completion within a bounded number of
    attempts. Account and rate failures become provider blocks; documented media
    absence becomes unavailable; malformed payloads and transport faults remain
    transient.
    """

    @property
    def source(self) -> str:
        """The provenance tag for transcripts fetched through Supadata."""
        return _SOURCE

    def __init__(
        self,
        transport: SupadataTransport,
        *,
        config: SupadataConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._transport: SupadataTransport = transport
        self._config: SupadataConfig = config or SupadataConfig()
        # Injectable so tests need not wait real seconds between poll attempts.
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        # A monotonic clock (injectable for tests) drives the request-pacing gate;
        # the provider is long-lived, so it remembers the last submit across fetches.
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._last_request_at: float | None = None

    async def _throttle(self) -> None:
        """Wait out the min-interval since the previous submit, if one is configured.

        Enforces at most one billed submit per `min_request_interval` so the worker's
        back-to-back fetches stay under a low-rate plan's per-request limit. A no-op
        when pacing is disabled (zero interval) or when the interval has already
        elapsed; the timestamp is stamped after any wait so it reflects the actual
        request time.
        """
        interval = self._config.min_request_interval.total_seconds()
        if interval <= 0:
            return
        if self._last_request_at is not None:
            wait = interval - (self._monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = self._monotonic()

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        """Fetch a transcript via Supadata, polling accepted jobs to completion."""
        # Pace submits to stay under the plan's per-request rate limit.
        await self._throttle()
        submission = (await self._transport.submit(video_id)).map_error(
            partial(_classify_transport_failure, video_id)
        )
        return await submission.and_then_async(
            partial(self._resolve_submission, video_id)
        )

    async def _resolve_submission(
        self, video_id: str, response: SupadataSubmitResponse
    ) -> TranscriptFetchResult:
        """Resolve an immediate transcript or continue an accepted async job."""
        if isinstance(response, SupadataJobAccepted):
            return await self._poll_to_completion(video_id, response.job_id)
        return _extract_transcript(response, video_id=video_id)

    async def _poll_to_completion(
        self, video_id: str, job_id: str
    ) -> TranscriptFetchResult:
        """Poll an async job up to `max_poll_attempts`, resolving its terminal state.

        A completed job's content becomes the transcript; a failed or empty job is
        *unavailable*; a rate limit while polling is *blocked*; and a job still
        pending after the attempt budget is *transient* (retried per target next
        pass) rather than hanging the worker.
        """
        polling: _SupadataPollingResult = Ok(None)
        for _ in range(self._config.max_poll_attempts):
            polling = await polling.and_then_async(
                partial(self._continue_polling, video_id, job_id)
            )
        return polling.and_then(partial(self._finish_polling, video_id, job_id))

    async def _continue_polling(
        self,
        video_id: str,
        job_id: str,
        transcript: FetchedTranscript | None,
    ) -> _SupadataPollingResult:
        """Preserve a completion or make the next bounded poll request."""
        if transcript is not None:
            return Ok(transcript)
        await self._sleep(self._config.poll_interval.total_seconds())
        return (
            (await self._transport.poll(job_id))
            .map_error(partial(_classify_transport_failure, video_id))
            .and_then(partial(self._resolve_poll_response, video_id))
        )

    def _resolve_poll_response(
        self, video_id: str, response: SupadataPollResponse
    ) -> _SupadataPollingResult:
        """Resolve one validated poll response into completion or continuation."""
        if isinstance(response, SupadataJobFailed):
            return Err(_classify_job_failure(video_id, response))
        if isinstance(response, SupadataJobCompleted):
            return _extract_transcript(response, video_id=video_id)
        return Ok(None)

    def _finish_polling(
        self,
        video_id: str,
        job_id: str,
        transcript: FetchedTranscript | None,
    ) -> TranscriptFetchResult:
        """Return a completion or classify the exhausted pending job."""
        if transcript is not None:
            return Ok(transcript)
        return Err(
            _unfinished_failure(video_id, job_id, self._config.max_poll_attempts)
        )

    async def aclose(self) -> None:
        """Close the transport when it owns async resources."""
        if isinstance(self._transport, AsyncClosable):
            await self._transport.aclose()


def _is_http_failure(response: httpx2.Response) -> bool:
    """Treat Supadata's special 206 unavailable status as a failure everywhere."""
    return (
        response.status_code == httpx2.codes.PARTIAL_CONTENT.value
        or not httpx2.codes.is_success(response.status_code)
    )


def _http_failure(
    response: httpx2.Response, *, operation: SupadataOperation
) -> SupadataHttpFailure:
    """Normalize an unsuccessful response without trusting its optional JSON body."""
    try:
        error = _ERROR_PAYLOAD_ADAPTER.validate_json(response.content, strict=True)
    except ValidationError:
        error = None
    return SupadataHttpFailure(
        operation=operation,
        status_code=response.status_code,
        error=error,
        retry_after=_retry_after_seconds(response.headers),
    )


def _classify_http_response(
    response: httpx2.Response, *, operation: SupadataOperation
) -> Result[httpx2.Response, SupadataHttpFailure]:
    """Keep successful HTTP responses and normalize non-success responses."""
    if _is_http_failure(response):
        return Err(_http_failure(response, operation=operation))
    return Ok(response)


def _decode_success[T](
    response: httpx2.Response,
    *,
    adapter: TypeAdapter[T],
    operation: SupadataOperation,
) -> Result[T, SupadataProtocolFailure]:
    """Validate a successful response or report upstream schema drift."""
    try:
        return Ok(adapter.validate_json(response.content, strict=True))
    except ValidationError:
        return Err(SupadataProtocolFailure(operation=operation))


class HttpSupadataTransport:
    """Async HTTP transport that validates every successful Supadata payload."""

    def __init__(
        self,
        api_key: NonEmptySecretStr,
        *,
        config: SupadataConfig | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        selected_config = config or SupadataConfig()
        self._config: SupadataConfig = selected_config
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=selected_config.base_url,
            headers={"x-api-key": api_key.get_secret_value()},
            timeout=selected_config.timeout.total_seconds(),
            transport=http_transport,
        )

    async def submit(self, locator: str) -> SupadataSubmitResult:
        """Submit and decode either a direct transcript or accepted job."""
        params = {
            "url": locator,
            "mode": "native",
        }
        if self._config.languages:
            params["lang"] = self._config.languages[0]
        try:
            response = await self._client.get(
                "/transcript",
                params=params,
            )
        except httpx2.RequestError as e:
            return Err(SupadataNetworkFailure(operation="submit", message=str(e)))
        return _classify_http_response(response, operation="submit").and_then(
            partial(
                _decode_success,
                adapter=_SUBMIT_RESPONSE_ADAPTER,
                operation="submit",
            )
        )

    async def poll(self, job_id: str) -> SupadataPollResult:
        """Poll and decode one known asynchronous job state."""
        try:
            response = await self._client.get(f"/transcript/{job_id}")
        except httpx2.RequestError as e:
            return Err(SupadataNetworkFailure(operation="poll", message=str(e)))
        return _classify_http_response(response, operation="poll").and_then(
            partial(
                _decode_success,
                adapter=_POLL_RESPONSE_ADAPTER,
                operation="poll",
            )
        )

    async def aclose(self) -> None:
        """Close the reusable HTTP connection pool."""
        await self._client.aclose()


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` header into a timedelta, if present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    return None
