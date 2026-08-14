"""The paid, flag-gated `TranscriptProvider` backed by Supadata.

The OAuth captions Data API is owner-only — it 403s for nearly every third-party
(liked) video — and the free `youtube-transcript-api` library is IP-block-prone,
so on their own they transcribe almost none of the liked corpus. Supadata is an
HTTP transcript API (with an API key, billed per call) that reliably does, so when
it is configured it becomes the *primary* source (see
`tether.transcripts.provider_composition`); the free providers trail it as
best-effort fallbacks.

This wraps Supadata behind the `TranscriptProvider` port so the composite
(`FallbackTranscriptProvider`) slots it in with no structural change to the
worker. Three pieces of resilience matter:

* It is gated — composed into the chain only when an API key is configured *and*
  the feature flag is on, so the default install never spends and stays
  offline-friendly.
* It reuses the per-source provider-pause pattern with its own ``"supadata"``
  source key: a Supadata rate limit maps to the *blocked* outcome (carrying any
  retry-after hint), so hitting Supadata's limits pauses *only* Supadata while the
  free providers keep working.
* A `SupadataSpendGuard` enforces a hard, persisted cap on total uses: each call
  reserves one use before spending, and an exhausted cap raises the same *blocked*
  outcome, so a bounded plan (e.g. 100 uses) stops the background sweep instead of
  overspending. `mode=native` keeps every call to a single, cheap lookup — never
  the multi-use AI `generate` path.

Supadata serves long videos via an async job model (submit returns a `jobId`,
poll it to completion), so `fetch` submits, then polls at a bounded interval up to
a max attempt count rather than blocking the worker indefinitely. The HTTP layer
is a `SupadataTransport` seam faked in tests, so no test spends money or hits the
network; the submit/poll/extract logic is pure over the response payloads and is
unit-tested against fixtures.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol

import httpx2
import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from snekok import Err, Ok, Result
from snekok.types import NonBlankStr, NonEmptySecretStr, NonNegativeInt
from snekql.sqlite import Database, Transaction, insert, select, update

from tether.youtube import (
    Clock,
    FetchedTranscript,
    SourceUsage,
    SystemClock,
    TranscriptBlockedError,
    TranscriptProvider,
    TranscriptSegment,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeSyncState,
    find_transcript_provider_leaves,
)

# TODO: want to move this inside a class, but need to do some consolidation first
_SOURCE = "supadata"
"""The provenance tag stamped onto Supadata transcripts and its pause-state key."""
_NO_PAUSED_SOURCES: frozenset[str] = frozenset()
"""The empty default for `fetch`'s `paused_sources` — Supadata is a leaf source."""

_SPEND_KEY_PREFIX = "supadata_uses"
"""Prefix of the `YouTubeSyncState` keys holding the Supadata use count.

The count is bucketed by UTC calendar month (`supadata_uses:YYYY-MM`), mirroring
the daily YouTube-quota pattern, so the cap is a *monthly* budget that resets at
the month boundary. Persisted (not per-process) so a frequent `just dev` restart
does not hand the sweep a fresh budget every boot. The local month may not align
with Supadata's own billing month, so the counter is a conservative floor."""


def _month_key(now: datetime) -> str:
    """The spend-counter key for the UTC calendar month containing `now`."""
    return f"{_SPEND_KEY_PREFIX}:{now.astimezone(UTC):%Y-%m}"


def _start_of_next_month(now: datetime) -> datetime:
    """The first instant of the UTC month after `now` — when a monthly cap resets."""
    moment = now.astimezone(UTC)
    if moment.month == 12:  # noqa: PLR2004
        return datetime(moment.year + 1, 1, 1, tzinfo=UTC)
    return datetime(moment.year, moment.month + 1, 1, tzinfo=UTC)


SupadataMode = Literal["native", "generate"]
"""Supadata's transcript modes: `native` fetches an existing caption track (one
use); `generate` runs multi-use AI transcription. Tether pins `native`."""


class SupadataConfigurationError(Exception):
    """The Supadata provider was built without a usable API key."""


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
    duration: NonNegativeInt | None = None
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


class SupadataJobFailed(_SupadataPayload):
    """An async transcript job that Supadata could not complete."""

    status: Literal["failed"]


class _SupadataErrorPayload(_SupadataPayload):
    """The stable error field Tether uses for Supadata classification."""

    error: str | None = None


@dataclass(frozen=True, slots=True)
class SupadataHttpFailure:
    """A non-success Supadata response with normalized classification data."""

    status_code: int
    error: str | None = None
    retry_after: timedelta | None = None


type SupadataSubmitResponse = (
    SupadataTranscript | SupadataJobAccepted | SupadataHttpFailure
)
type SupadataPollResponse = (
    SupadataJobPending | SupadataJobCompleted | SupadataJobFailed | SupadataHttpFailure
)


class SupadataTransport(Protocol):
    """The typed Supadata HTTP boundary driven by the transcript provider."""

    async def submit(self, video_id: str) -> SupadataSubmitResponse:
        """Request an immediate transcript or an asynchronous job."""
        ...

    async def poll(self, job_id: str) -> SupadataPollResponse:
        """Read the current state of an asynchronous transcript job."""
        ...


class SupadataProtocolError(Exception):
    """Supadata returned a successful HTTP response with an invalid payload."""


_SUBMIT_RESPONSE_ADAPTER: TypeAdapter[SupadataTranscript | SupadataJobAccepted] = (
    TypeAdapter(SupadataTranscript | SupadataJobAccepted)
)
_POLL_RESPONSE_ADAPTER: TypeAdapter[
    SupadataJobPending | SupadataJobCompleted | SupadataJobFailed
] = TypeAdapter(SupadataJobPending | SupadataJobCompleted | SupadataJobFailed)
_ERROR_PAYLOAD_ADAPTER: TypeAdapter[_SupadataErrorPayload] = TypeAdapter(
    _SupadataErrorPayload
)


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
    mode: SupadataMode = "native"
    """Supadata transcript mode sent on every submit. `native` fetches an existing
    caption track only — one Supadata use per call — and returns *unavailable* for a
    caption-less video rather than silently falling through to the multi-use AI
    `generate` path. Pinned so a bounded plan (e.g. 100 uses) is spent one lookup at
    a time and never surprise-billed for a generation."""


def _is_rate_limited(response: SupadataHttpFailure) -> bool:
    """Whether a failure is Supadata's provider-level rate or quota signal."""
    if response.status_code == httpx2.codes.TOO_MANY_REQUESTS.value:
        return True
    if response.error is None:
        return False
    marker = response.error.lower()
    return "limit" in marker or "rate" in marker or "quota" in marker


def _is_unavailable(response: SupadataHttpFailure) -> bool:
    """Whether a failure means Supadata permanently lacks this transcript."""
    if response.status_code in {
        httpx2.codes.FORBIDDEN.value,
        httpx2.codes.NOT_FOUND.value,
        httpx2.codes.PARTIAL_CONTENT.value,
    }:
        return True
    if response.error is None:
        return False
    marker = response.error.lower()
    return "transcript" in marker and (
        "unavailable" in marker or "not-found" in marker or "not found" in marker
    )


def _extract_transcript(
    transcript: SupadataTranscript | SupadataJobCompleted,
) -> tuple[str, tuple[TranscriptSegment, ...]] | None:
    """Convert one validated Supadata transcript into Tether's domain shape."""
    if isinstance(transcript.content, str):
        cleaned = transcript.content.strip()
        return (cleaned, ()) if cleaned else None
    if not transcript.content:
        return None
    segments = tuple(
        TranscriptSegment(
            start_seconds=cue.offset / 1000.0,
            text=cue.text.strip(),
        )
        for cue in transcript.content
    )
    return " ".join(segment.text for segment in segments), segments


def _unfinished_error(
    video_id: str, job_id: str, attempts: int
) -> TranscriptTransientError:
    """Build the transient failure for a job exceeding its bounded poll budget."""
    return TranscriptTransientError(
        f"supadata job {job_id} for {video_id} unfinished after {attempts} polls"
    )


def _classify_failure(
    video_id: str, response: SupadataHttpFailure
) -> Err[
    TranscriptBlockedError | TranscriptUnavailableError | TranscriptTransientError
]:
    """Map a non-success Supadata response onto a typed `TranscriptProvider` signal.

    Rate limits are the *blocked* outcome (carrying any retry-after hint so the
    worker's Supadata pause honors it); a forbidden or missing transcript is
    *unavailable* (terminal); everything else — 5xx, malformed bodies — is
    *transient*.
    """
    if _is_rate_limited(response):
        return Err(
            TranscriptBlockedError(
                f"supadata rate-limited for {video_id}",
                retry_after=response.retry_after,
                source=_SOURCE,
            )
        )
    if _is_unavailable(response):
        return Err(TranscriptUnavailableError(video_id))
    return Err(
        TranscriptTransientError(
            f"supadata fetch for {video_id} failed (status {response.status_code})"
        )
    )


class SupadataBudgetExhaustedError(Exception):
    """The Supadata monthly use cap is reached, so no further call may be billed.

    Raised by a `SupadataSpendGuard` *before* any HTTP call, carrying the spent
    count, the cap, and the time until the monthly budget resets. The provider
    translates it into the *blocked* outcome so the worker pauses Supadata until
    the month boundary and leaves videos pending.
    """

    def __init__(
        self, used: int, limit: int, *, retry_after: timedelta | None = None
    ) -> None:
        super().__init__(
            f"supadata monthly use cap reached ({used}/{limit}); resets at the month boundary"
        )
        self.used: int = used
        self.limit: int = limit
        self.retry_after: timedelta | None = retry_after


class SupadataSpendGuard:
    """A hard, persisted *monthly* cap on Supadata uses spanning restarts.

    The count lives in `YouTubeSyncState` under the current month's key
    (`supadata_uses:YYYY-MM`); `charge` reads, checks against `max_uses`, and
    increments in a single transaction, so a serial worker never exceeds the cap.
    A new UTC month starts with no row and therefore a fresh budget. The check runs
    *before* the billed call and the increment persists on success, so a crash
    between reserving and calling over-counts (safe) rather than over-spends.
    Single-tenant Tether has no concurrent charger, so the read-then-write needs no
    extra locking beyond the transaction.
    """

    def __init__(
        self, database: Database, *, max_uses: int, clock: Clock | None = None
    ) -> None:
        self._database: Database = database
        self._max_uses: int = max(0, max_uses)
        self._clock: Clock = clock or SystemClock()

    async def charge(self) -> Result[None, SupadataBudgetExhaustedError]:
        """Reserve one use within the month's cap, or raise when it is exhausted."""
        now = self._clock.now()
        month_key = _month_key(now)

        async def _reserve(
            tx: Transaction,
        ) -> Result[None, SupadataBudgetExhaustedError]:
            row = await tx.fetch_one_or_none(
                select(YouTubeSyncState).where(YouTubeSyncState.key.eq(month_key))
            )
            used = int(row.value) if row is not None else 0
            if used >= self._max_uses:
                return Err(
                    SupadataBudgetExhaustedError(
                        used,
                        self._max_uses,
                        retry_after=_start_of_next_month(now) - now,
                    )
                )
            spent = str(used + 1)
            if row is None:
                _ = await tx.execute(
                    insert(YouTubeSyncState(key=month_key, value=spent))
                )
            else:
                _ = await tx.execute(
                    update(YouTubeSyncState)
                    .set(YouTubeSyncState.value.to(spent))
                    .where(YouTubeSyncState.key.eq(month_key))
                )
            return Ok(None)

        async with self._database.transaction(mode="immediate") as tx:
            match await _reserve(tx):
                case Ok(None):
                    pass
                case Err(error):
                    return Err(error)
        return Ok(None)

    async def snapshot(self, *, now: datetime) -> SourceUsage:
        """Report the current UTC month's usage against the cap, without charging."""
        month_key = _month_key(now)
        async with self._database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(YouTubeSyncState).where(YouTubeSyncState.key.eq(month_key))
            )
        used = int(row.value) if row is not None else 0
        return SourceUsage(
            used=used,
            limit=self._max_uses,
            remaining=max(0, self._max_uses - used),
            period=month_key.removeprefix(f"{_SPEND_KEY_PREFIX}:"),
        )


def bind_supadata_spend_guard(
    provider: TranscriptProvider,
    database: Database,
    *,
    max_uses: int,
    clock: Clock | None = None,
) -> None:
    """Late-bind a persisted monthly cap onto every Supadata provider in a chain.

    The provider tree is built from settings before the database exists, so the
    hard cap is attached here (at wire time) the same way the semantic-search
    collaborator is. Uses the generic `find_transcript_provider_leaves` walk (by
    the `"supadata"` source tag) so a Supadata primary *or* fallback is covered;
    a no-op when the chain has no Supadata.
    """
    for leaf in find_transcript_provider_leaves(provider, source=_SOURCE):
        if isinstance(leaf, SupadataTranscriptProvider):
            leaf.spend_guard = SupadataSpendGuard(
                database, max_uses=max_uses, clock=clock
            )


class SupadataTranscriptProvider(TranscriptProvider):
    """The paid `TranscriptProvider` backed by Supadata (the primary when enabled).

    Enabled only when key + flag are set, in which case it leads the chain. It
    reserves one use from its `SupadataSpendGuard` (raising *blocked* at the cap),
    then submits a transcript request, returns immediately on a direct hit, and
    otherwise polls the async job to completion within a bounded number of
    attempts. A rate limit is the distinct *blocked* signal that trips the worker's
    Supadata-specific pause; no transcript is *unavailable*; an exhausted-poll or
    transport error is *transient*. Its spend is bounded by the guard's persisted
    use cap, separate from the YouTube daily-unit budget.
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
        spend_guard: SupadataSpendGuard,
    ) -> None:
        self._transport: SupadataTransport = transport
        self._config: SupadataConfig = config or SupadataConfig()
        # Injectable so tests need not wait real seconds between poll attempts.
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        # A monotonic clock (injectable for tests) drives the request-pacing gate;
        # the provider is long-lived, so it remembers the last submit across fetches.
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._last_request_at: float | None = None
        # Public so the wiring can late-bind the persisted cap once the database
        # exists (the tree is built from settings first); unbounded by default.
        self.spend_guard: SupadataSpendGuard = spend_guard

    async def usage_snapshot(self, *, now: datetime) -> SourceUsage | None:
        """This leaf's `UsageReportingProvider` capability: the bound guard's own
        snapshot, or None when no persisted cap has been bound yet."""
        return await self.spend_guard.snapshot(now=now)

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

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> Result[
        FetchedTranscript,
        TranscriptBlockedError | TranscriptUnavailableError | TranscriptTransientError,
    ]:
        """Fetch a transcript via Supadata (direct or async job), or raise a signal.

        Supadata is a leaf source the composite skips while paused or gated, so
        `paused_sources` and `skip_sources` are no-ops here.

        Reserves one guarded use before the billed call; an exhausted cap is the
        *blocked* outcome (Supadata's own source), so the worker pauses Supadata
        and leaves the video pending rather than spending past the plan.
        """
        # TODO: oof
        _ = (paused_sources, skip_sources)
        match await self.spend_guard.charge():
            case Ok(None):
                pass
            case Err(error):
                structlog.stdlib.get_logger("tether.transcripts.supadata").warning(
                    "Supadata monthly use cap exhausted; pausing until reset",
                    used=error.used,
                    limit=error.limit,
                )
                return Err(
                    TranscriptBlockedError(
                        str(error), retry_after=error.retry_after, source=_SOURCE
                    )
                )
        # Pace the billed submit to stay under the plan's per-request rate limit.
        await self._throttle()
        try:
            response = await self._transport.submit(video_id)
        except httpx2.RequestError, SupadataProtocolError:
            # Transport faults and upstream schema drift are retryable. Neither
            # proves that this video permanently lacks a transcript.
            return Err(TranscriptTransientError(video_id))
        if isinstance(response, SupadataHttpFailure):
            return _classify_failure(video_id, response)
        if isinstance(response, SupadataJobAccepted):
            return await self._poll_to_completion(video_id, response.job_id)
        extracted = _extract_transcript(response)
        if extracted is None:
            return Err(TranscriptUnavailableError(video_id))
        text, segments = extracted
        return Ok(FetchedTranscript(text=text, segments=segments, source=_SOURCE))

    async def _poll_to_completion(
        self, video_id: str, job_id: str
    ) -> Result[
        FetchedTranscript,
        TranscriptBlockedError | TranscriptUnavailableError | TranscriptTransientError,
    ]:
        """Poll an async job up to `max_poll_attempts`, resolving its terminal state.

        A completed job's content becomes the transcript; a failed or empty job is
        *unavailable*; a rate limit while polling is *blocked*; and a job still
        pending after the attempt budget is *transient* (retried per-video next
        pass) rather than hanging the worker.
        """
        for _ in range(self._config.max_poll_attempts):
            await self._sleep(self._config.poll_interval.total_seconds())
            try:
                response = await self._transport.poll(job_id)
            except httpx2.RequestError, SupadataProtocolError:
                return Err(TranscriptTransientError(video_id))
            if isinstance(response, SupadataHttpFailure):
                return _classify_failure(video_id, response)
            if isinstance(response, SupadataJobFailed):
                return Err(TranscriptUnavailableError(video_id))
            if isinstance(response, SupadataJobCompleted):
                extracted = _extract_transcript(response)
                if extracted is None:
                    return Err(TranscriptUnavailableError(video_id))
                text, segments = extracted
                return Ok(
                    FetchedTranscript(text=text, segments=segments, source=_SOURCE)
                )
            # A validated pending state consumes one bounded poll attempt.
        return Err(_unfinished_error(video_id, job_id, self._config.max_poll_attempts))


def _video_url(video_id: str) -> str:
    """Build the canonical YouTube watch URL Supadata requires."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _submit_params(
    video_id: str, mode: SupadataMode, languages: tuple[str, ...] = ()
) -> dict[str, str]:
    """Build the explicit billed mode and preferred-language submit parameters."""
    params = {"url": _video_url(video_id), "mode": mode}
    if languages:
        params["lang"] = languages[0]
    return params


def _is_http_failure(response: httpx2.Response) -> bool:
    """Treat Supadata's special 206 unavailable status as a failure everywhere."""
    return (
        response.status_code == httpx2.codes.PARTIAL_CONTENT.value
        or not httpx2.codes.is_success(response.status_code)
    )


def _http_failure(response: httpx2.Response) -> SupadataHttpFailure:
    """Normalize an unsuccessful response without trusting its optional JSON body."""
    try:
        payload = _ERROR_PAYLOAD_ADAPTER.validate_json(response.content, strict=True)
    except ValidationError:
        payload = _SupadataErrorPayload()
    return SupadataHttpFailure(
        status_code=response.status_code,
        error=payload.error,
        retry_after=_retry_after_seconds(response.headers),
    )


def _decode_success[T](
    response: httpx2.Response,
    *,
    adapter: TypeAdapter[T],
    operation: str,
) -> T:
    """Validate a successful response or report upstream schema drift."""
    try:
        return adapter.validate_json(response.content, strict=True)
    except ValidationError as error:
        message = f"Supadata {operation} returned an invalid success payload"
        raise SupadataProtocolError(message) from error


class HttpSupadataTransport(SupadataTransport):
    """Async HTTP transport that validates every successful Supadata payload."""

    def __init__(
        self,
        api_key: NonEmptySecretStr,
        *,
        config: SupadataConfig | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        revealed_api_key = api_key.get_secret_value()
        if not revealed_api_key:
            message = "Supadata API key is required to build the HTTP transport"
            raise SupadataConfigurationError(message)
        selected_config = config or SupadataConfig()
        self._config: SupadataConfig = selected_config
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=selected_config.base_url,
            headers={"x-api-key": revealed_api_key},
            timeout=selected_config.timeout.total_seconds(),
            transport=http_transport,
        )

    async def submit(self, video_id: str) -> SupadataSubmitResponse:
        """Submit and decode either a direct transcript or accepted job."""
        response = await self._client.get(
            "/transcript",
            params=_submit_params(video_id, self._config.mode, self._config.languages),
        )
        if _is_http_failure(response):
            return _http_failure(response)
        return _decode_success(
            response,
            adapter=_SUBMIT_RESPONSE_ADAPTER,
            operation="submit",
        )

    async def poll(self, job_id: str) -> SupadataPollResponse:
        """Poll and decode one known asynchronous job state."""
        response = await self._client.get(f"/transcript/{job_id}")
        if _is_http_failure(response):
            return _http_failure(response)
        return _decode_success(
            response,
            adapter=_POLL_RESPONSE_ADAPTER,
            operation="poll",
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
