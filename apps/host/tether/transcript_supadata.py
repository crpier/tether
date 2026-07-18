"""The paid, flag-gated `TranscriptProvider` backed by Supadata.

The OAuth captions Data API is owner-only — it 403s for nearly every third-party
(liked) video — and the free `youtube-transcript-api` library is IP-block-prone,
so on their own they transcribe almost none of the liked corpus. Supadata is an
HTTP transcript API (with an API key, billed per call) that reliably does, so when
it is configured it becomes the *primary* source (see
`tether.transcript_provider_composition`); the free providers trail it as
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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

import httpx2
import structlog
from snekql.sqlite import Database, Transaction, insert, select, update

from tether.db_retry import run_in_transaction
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


_LAST_MONTH_OF_YEAR = 12
"""December — a December `now` rolls the monthly cap over into the next January."""


def _start_of_next_month(now: datetime) -> datetime:
    """The first instant of the UTC month after `now` — when a monthly cap resets."""
    moment = now.astimezone(UTC)
    if moment.month == _LAST_MONTH_OF_YEAR:
        return datetime(moment.year + 1, 1, 1, tzinfo=UTC)
    return datetime(moment.year, moment.month + 1, 1, tzinfo=UTC)


_HTTP_TOO_MANY_REQUESTS = 429
"""Supadata's rate-limit / quota status — the *blocked* outcome."""
_HTTP_NOT_FOUND = 404
"""Supadata's "no transcript for this video" status — the *unavailable* outcome."""
_HTTP_CLIENT_ERROR_FLOOR = 400
"""Any status at or above this is an error response to classify, not a transcript."""

# Supadata async-job terminal states (`GET /v1/transcript/{jobId}` -> `status`); any
# other status (active, queued, starting, ...) is still pending and keeps polling.
_JOB_COMPLETED = "completed"
_JOB_FAILED = "failed"


SupadataMode = Literal["native", "generate"]
"""Supadata's transcript modes: `native` fetches an existing caption track (one
use); `generate` runs multi-use AI transcription. Tether pins `native`."""


class SupadataConfigurationError(Exception):
    """The Supadata provider was built without a usable API key."""


@dataclass(frozen=True, slots=True)
class SupadataResponse:
    """One Supadata HTTP response, normalized for the pure interpretation logic.

    `payload` is the decoded JSON body; `retry_after` is any parsed `Retry-After`
    hint (only meaningful on a 429). Keeping the transport's output this small is
    what lets the submit/poll/extract logic be unit-tested without httpx.
    """

    status_code: int
    payload: Mapping[str, object]
    retry_after: timedelta | None = None


class SupadataTransport(Protocol):
    """The isolated Supadata HTTP boundary the provider drives.

    Two calls: `submit` requests a transcript (which may resolve immediately or
    hand back a job id), and `poll` checks an async job. Faked in tests so the
    provider's logic is exercised offline.
    """

    async def submit(self, video_id: str) -> SupadataResponse:
        """Request a transcript for a video (sync result or an async job id)."""
        ...

    async def poll(self, job_id: str) -> SupadataResponse:
        """Check an async transcript job's status (and content once complete)."""
        ...


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


def _is_rate_limited(response: SupadataResponse) -> bool:
    """Whether a response is Supadata's rate-limit / quota signal (the *blocked* outcome)."""
    if response.status_code == _HTTP_TOO_MANY_REQUESTS:
        return True
    error = response.payload.get("error")
    if isinstance(error, str):
        marker = error.lower()
        return "limit" in marker or "rate" in marker or "quota" in marker
    return False


def _is_unavailable(response: SupadataResponse) -> bool:
    """Whether a response means Supadata has no transcript (terminal *unavailable*)."""
    if response.status_code == _HTTP_NOT_FOUND:
        return True
    error = response.payload.get("error")
    if isinstance(error, str):
        marker = error.lower()
        return "transcript" in marker and (
            "unavailable" in marker or "not-found" in marker or "not found" in marker
        )
    return False


def _extract_transcript(
    payload: Mapping[str, object],
) -> tuple[str, tuple[TranscriptSegment, ...]] | None:
    """Parse Supadata `content` into joined text plus timed segments, or None.

    Supadata returns `content` either as a plain string (when text-only) or as a
    list of ``{text, offset}`` cues (offset in milliseconds). Empty or untimed cues
    are dropped; ``None`` signals "no usable transcript in this payload".
    """
    content = payload.get("content")
    if isinstance(content, str):
        cleaned = content.strip()
        return (cleaned, ()) if cleaned else None
    if isinstance(content, Iterable):
        segments = _parse_cues(cast("Iterable[object]", content))
        if not segments:
            return None
        joined = " ".join(segment.text for segment in segments)
        return joined, segments
    return None


def _parse_cues(cues: Iterable[object]) -> tuple[TranscriptSegment, ...]:
    """Parse Supadata's timed cues into `TranscriptSegment`s (offset ms -> seconds)."""
    segments: list[TranscriptSegment] = []
    for cue in cues:
        if not isinstance(cue, Mapping):
            continue
        mapping = cast("Mapping[str, object]", cue)
        text = mapping.get("text")
        if not isinstance(text, str):
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        offset = mapping.get("offset")
        start_seconds = (
            float(offset) / 1000.0 if isinstance(offset, (int, float)) else 0.0
        )
        segments.append(TranscriptSegment(start_seconds=start_seconds, text=cleaned))
    return tuple(segments)


def _job_id(payload: Mapping[str, object]) -> str | None:
    """The async job id Supadata hands back for a long-running transcript, if any."""
    job_id = payload.get("jobId")
    return job_id if isinstance(job_id, str) and job_id else None


def _classify_failure(video_id: str, response: SupadataResponse) -> Exception:
    """Map a non-success Supadata response onto a typed `TranscriptProvider` signal.

    Rate limits are the *blocked* outcome (carrying any retry-after hint so the
    worker's Supadata pause honors it); a missing transcript is *unavailable*
    (terminal); everything else — 5xx, malformed bodies — is *transient*.
    """
    if _is_rate_limited(response):
        return TranscriptBlockedError(
            f"supadata rate-limited for {video_id}",
            retry_after=response.retry_after,
            source=_SOURCE,
        )
    if _is_unavailable(response):
        return TranscriptUnavailableError(video_id)
    return TranscriptTransientError(
        f"supadata fetch for {video_id} failed (status {response.status_code})"
    )


def _unfinished_error(
    video_id: str, job_id: str, attempts: int
) -> TranscriptTransientError:
    """The *transient* signal for a job still pending after the poll budget."""
    return TranscriptTransientError(
        f"supadata job {job_id} for {video_id} unfinished after {attempts} polls"
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


class SupadataSpendGuard(Protocol):
    """Reserves one Supadata use before a billed call, enforcing a hard cap."""

    async def charge(self) -> None:
        """Reserve one use, or raise `SupadataBudgetExhaustedError` if the cap is hit."""
        ...

    async def snapshot(self, *, now: datetime) -> SourceUsage | None:
        """The current month's usage without charging, or None when uncapped."""
        ...


class UnlimitedSupadataSpend(SupadataSpendGuard):
    """The default guard: never caps (used in tests and when no cap is wired)."""

    async def charge(self) -> None:
        """A no-op — spending is unbounded."""

    async def snapshot(self, *, now: datetime) -> SourceUsage | None:
        """None — there is no cap to report usage against."""
        _ = now
        return None


class PersistentSupadataSpendGuard(SupadataSpendGuard):
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

    async def charge(self) -> None:
        """Reserve one use within the month's cap, or raise when it is exhausted."""
        now = self._clock.now()
        month_key = _month_key(now)

        async def _reserve(tx: Transaction) -> None:
            row = await tx.fetch_one_or_none(
                select(YouTubeSyncState).where(YouTubeSyncState.key.eq(month_key))
            )
            used = int(row.value) if row is not None else 0
            if used >= self._max_uses:
                raise SupadataBudgetExhaustedError(
                    used,
                    self._max_uses,
                    retry_after=_start_of_next_month(now) - now,
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

        await run_in_transaction(self._database, _reserve)

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
            leaf.spend_guard = PersistentSupadataSpendGuard(
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

    source: str = _SOURCE

    def __init__(
        self,
        transport: SupadataTransport,
        *,
        config: SupadataConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        spend_guard: SupadataSpendGuard | None = None,
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
        self.spend_guard: SupadataSpendGuard = spend_guard or UnlimitedSupadataSpend()

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
    ) -> FetchedTranscript:
        """Fetch a transcript via Supadata (direct or async job), or raise a signal.

        Supadata is a leaf source the composite skips while paused or gated, so
        `paused_sources` and `skip_sources` are no-ops here.

        Reserves one guarded use before the billed call; an exhausted cap is the
        *blocked* outcome (Supadata's own source), so the worker pauses Supadata
        and leaves the video pending rather than spending past the plan.
        """
        _ = (paused_sources, skip_sources)
        try:
            await self.spend_guard.charge()
        except SupadataBudgetExhaustedError as error:
            # Loud: the paid monthly budget is spent. The worker pauses Supadata
            # until the month boundary (the carried retry-after), leaving videos
            # pending rather than overspending.
            structlog.stdlib.get_logger("tether.transcript_supadata").warning(
                "Supadata monthly use cap exhausted; pausing until reset",
                used=error.used,
                limit=error.limit,
            )
            raise TranscriptBlockedError(
                str(error), retry_after=error.retry_after, source=_SOURCE
            ) from error
        # Pace the billed submit to stay under the plan's per-request rate limit.
        await self._throttle()
        response = await self._transport.submit(video_id)
        # A rate limit or any client/server error is a failure to classify; a 2xx is
        # either a job handoff, a direct transcript, or a genuine "no transcript".
        if (
            _is_rate_limited(response)
            or response.status_code >= _HTTP_CLIENT_ERROR_FLOOR
        ):
            raise _classify_failure(video_id, response)
        job_id = _job_id(response.payload)
        if job_id is not None:
            return await self._poll_to_completion(video_id, job_id)
        extracted = _extract_transcript(response.payload)
        if extracted is None:
            raise TranscriptUnavailableError(video_id)
        text, segments = extracted
        return FetchedTranscript(text=text, segments=segments, source=_SOURCE)

    async def _poll_to_completion(
        self, video_id: str, job_id: str
    ) -> FetchedTranscript:
        """Poll an async job up to `max_poll_attempts`, resolving its terminal state.

        A completed job's content becomes the transcript; a failed or empty job is
        *unavailable*; a rate limit while polling is *blocked*; and a job still
        pending after the attempt budget is *transient* (retried per-video next
        pass) rather than hanging the worker.
        """
        for _ in range(self._config.max_poll_attempts):
            await self._sleep(self._config.poll_interval.total_seconds())
            response = await self._transport.poll(job_id)
            if response.status_code >= _HTTP_CLIENT_ERROR_FLOOR:
                # A failed poll maps the same way a failed submit does.
                raise _classify_failure(video_id, response)
            status = response.payload.get("status")
            if status == _JOB_COMPLETED:
                extracted = _extract_transcript(response.payload)
                if extracted is None:
                    raise TranscriptUnavailableError(video_id)
                text, segments = extracted
                return FetchedTranscript(text=text, segments=segments, source=_SOURCE)
            if status == _JOB_FAILED:
                raise TranscriptUnavailableError(video_id)
            # Still pending — wait and poll again.
        raise _unfinished_error(video_id, job_id, self._config.max_poll_attempts)


def _video_url(video_id: str) -> str:
    """The canonical watch URL Supadata's `url` param requires for a YouTube video.

    Supadata's `/v1/transcript` validates `url` as a real URL and rejects a bare
    video id (`"url: Invalid url"`), so the id is expanded to a full watch URL.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def _submit_params(
    video_id: str, mode: SupadataMode, languages: tuple[str, ...] = ()
) -> dict[str, str]:
    """The query params for a Supadata transcript submit, pinning the billed mode.

    The video is sent as `url` (a full watch URL) because Supadata's endpoint
    requires it and 400s on the old `videoId` param (`"url: Required"`). `mode` is
    always sent so a caption-less video costs one `native` lookup and returns
    unavailable, never the multi-use `generate` path Supadata would pick when the
    param is omitted. The most preferred `languages` code, when set, rides along as
    `lang` so Supadata returns that track when it exists.
    """
    params = {"url": _video_url(video_id), "mode": mode}
    if languages:
        params["lang"] = languages[0]
    return params


class HttpSupadataTransport(SupadataTransport):
    """The production `SupadataTransport`: a thin httpx client over Supadata's API.

    Holds the API key and base URL; performs the GET requests and normalizes each
    into a `SupadataResponse`. All transcript semantics live in
    `SupadataTranscriptProvider`, keeping this boundary dumb and faked-in-tests.
    """

    def __init__(self, api_key: str, *, config: SupadataConfig | None = None) -> None:
        if not api_key:
            message = "Supadata API key is required to build the HTTP transport"
            raise SupadataConfigurationError(message)
        self._api_key: str = api_key
        self._config: SupadataConfig = config or SupadataConfig()

    async def submit(self, video_id: str) -> SupadataResponse:
        return await self._get(
            "/transcript",
            params=_submit_params(video_id, self._config.mode, self._config.languages),
        )

    async def poll(self, job_id: str) -> SupadataResponse:
        return await self._get(f"/transcript/{job_id}")

    async def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> SupadataResponse:
        timeout = self._config.timeout.total_seconds()
        async with httpx2.AsyncClient(
            base_url=self._config.base_url, timeout=timeout
        ) as client:
            response = await client.get(
                path, params=dict(params or {}), headers={"x-api-key": self._api_key}
            )
        return _from_httpx(response)


def _from_httpx(response: Any) -> SupadataResponse:
    """Normalize an httpx response into a `SupadataResponse` (decode JSON best-effort)."""
    try:
        body = response.json()
    except Exception:
        body = {}
    payload: Mapping[str, object] = (
        cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}
    )
    return SupadataResponse(
        status_code=int(response.status_code),
        payload=payload,
        retry_after=_retry_after_seconds(response.headers),
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` header into a timedelta, if present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    return None
