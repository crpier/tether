"""The paid, flag-gated last-resort `TranscriptProvider` backed by Supadata.

The captions provider and the free `youtube-transcript-api` library cover most
liked videos, but some yield nothing — the library gets IP-blocked for long
stretches, and a few videos are not covered by either free source. Supadata is an
HTTP transcript API (with an API key, billed per call) tried *last* so the few
videos the free sources miss can still get transcripts when the user has opted in
to spending for them.

This wraps Supadata behind the `TranscriptProvider` port so the composite
(`FallbackTranscriptProvider`) slots it in after the free providers with no
structural change to the worker. Two pieces of resilience matter:

* It is gated — composed into the chain only when an API key is configured *and*
  the feature flag is on (see `tether.server`), so the default install never spends
  and stays offline-friendly.
* It reuses the per-source provider-pause pattern with its own ``"supadata"``
  source key: a Supadata rate limit maps to the *blocked* outcome (carrying any
  retry-after hint), so hitting Supadata's limits pauses *only* Supadata while the
  free providers keep working.

Supadata serves long videos via an async job model (submit returns a `jobId`,
poll it to completion), so `fetch` submits, then polls at a bounded interval up to
a max attempt count rather than blocking the worker indefinitely. The HTTP layer
is a `SupadataTransport` seam faked in tests, so no test spends money or hits the
network; the submit/poll/extract logic is pure over the response payloads and is
unit-tested against fixtures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, cast

import httpx2

from tether.youtube import (
    FetchedTranscript,
    TranscriptBlockedError,
    TranscriptProvider,
    TranscriptSegment,
    TranscriptTransientError,
    TranscriptUnavailableError,
)

_SOURCE = "supadata"
"""The provenance tag stamped onto Supadata transcripts and its pause-state key."""
_NO_PAUSED_SOURCES: frozenset[str] = frozenset()
"""The empty default for `fetch`'s `paused_sources` — Supadata is a leaf source."""

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
    timeout: timedelta = timedelta(seconds=30)
    """Per-request HTTP timeout for both submit and poll."""
    poll_interval: timedelta = timedelta(seconds=2)
    """How long to wait between polls of an in-flight async transcript job."""
    max_poll_attempts: int = 10
    """Poll budget for an async job; exhausting it is *transient*, not a hang."""


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


class SupadataTranscriptProvider(TranscriptProvider):
    """The paid last-resort `TranscriptProvider` backed by Supadata.

    Composed behind the free providers (and only when key + flag enable it), so it
    is reached only after captions and the free library have failed. It submits a
    transcript request, returns immediately on a direct hit, and otherwise polls
    the async job to completion within a bounded number of attempts. A rate limit
    is the distinct *blocked* signal that trips the worker's Supadata-specific
    pause; no transcript is *unavailable*; an exhausted-poll or transport error is
    *transient*. It holds no budget — Supadata's cost is governed by the key + flag,
    not the YouTube daily-unit budget.
    """

    source: str = _SOURCE

    def __init__(
        self,
        transport: SupadataTransport,
        *,
        config: SupadataConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._transport: SupadataTransport = transport
        self._config: SupadataConfig = config or SupadataConfig()
        # Injectable so tests need not wait real seconds between poll attempts.
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

    async def fetch(
        self, video_id: str, *, paused_sources: frozenset[str] = _NO_PAUSED_SOURCES
    ) -> FetchedTranscript:
        """Fetch a transcript via Supadata (direct or async job), or raise a signal.

        Supadata is a leaf source the composite skips while paused, so
        `paused_sources` is a no-op here.
        """
        _ = paused_sources
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
        return await self._get("/transcript", params={"videoId": video_id})

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
