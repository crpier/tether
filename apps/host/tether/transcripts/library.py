"""Blocking `youtube-transcript-api` adapter behind a typed async source.

The optional library is imported lazily and runs in a worker thread. This
boundary normalizes its version-varying exceptions into unavailable, transient,
or blocked values and preserves `Retry-After` guidance. Request pacing remains
source-local; pass capacity belongs to acquisition orchestration.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast

from snekok import Err, Ok

from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptBlockedFailure,
    TranscriptFailure,
    TranscriptFetchResult,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)

_SOURCE = "youtube_transcript_api"

# Default (disabled) request pacing, hoisted for the same reason: a `timedelta(0)`
# default expression trips `reportCallInDefaultInitializer`.
_NO_PACING: timedelta = timedelta(0)


class TranscriptLibraryUnavailableError(Exception):
    """The optional `youtube-transcript-api` dependency is not installed."""


# The library's exception class *names* (matched across the MRO so subclasses
# count) mapped onto the typed `TranscriptProvider` outcomes. Names rather than
# imported types so classification is unit-testable against stand-in exceptions
# without importing the real library.
_IP_BLOCK_NAMES = frozenset({"RequestBlocked", "IpBlocked"})
_UNAVAILABLE_NAMES = frozenset(
    {
        "AgeRestricted",
        "TranscriptsDisabled",
        "NoTranscriptFound",
        "VideoUnavailable",
        "VideoUnplayable",
        "InvalidVideoId",
        "YouTubeDataUnparsable",
    }
)

# Message markers that betray an IP block / rate limit even when the class name is
# generic (older library versions, wrapped transport errors).
_IP_BLOCK_MARKERS = ("too many requests", "rate limit", "ip block", "ip has been")


def _mro_names(error: Exception) -> frozenset[str]:
    """The class names along an exception's MRO (so subclasses match by base)."""
    return frozenset(klass.__name__ for klass in type(error).__mro__)


def _is_transcript_ip_block_error(error: Exception) -> bool:
    """Whether a library error is an IP-block / rate-limit (the *blocked* outcome).

    Matches the library's `RequestBlocked` / `IpBlocked` by name (across the MRO,
    so `IpBlocked` counts via its `RequestBlocked` base) and, as a backstop for
    wrapped or older errors, telltale rate-limit phrases in the message.
    """
    if _mro_names(error) & _IP_BLOCK_NAMES:
        return True
    message = str(error).lower()
    return any(marker in message for marker in _IP_BLOCK_MARKERS) or "429" in message


def _find_http_response(error: Exception) -> Any | None:
    """Best-effort hunt for a `requests`-style response carrying headers.

    The library attaches the underlying transport error in different ways across
    versions (`response`, `http_error.response`, the chained `__cause__`), so this
    probes each rather than assuming one shape.
    """
    candidates: list[object | None] = [
        getattr(error, "response", None),
        getattr(getattr(error, "http_error", None), "response", None),
        getattr(error, "__cause__", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if hasattr(candidate, "headers"):
            return candidate
        nested = getattr(candidate, "response", None)
        if nested is not None and hasattr(nested, "headers"):
            return nested
    return None


def _retry_after_to_timedelta(value: object) -> timedelta | None:
    """Parse a `Retry-After` header value (delta-seconds or HTTP-date) to a delta."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return timedelta(seconds=int(text))
    try:
        when = parsedate_to_datetime(text)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = when - datetime.now(UTC)
    return delta if delta > timedelta(0) else None


def _parse_retry_after(error: Exception) -> timedelta | None:
    """Extract a `Retry-After` cooldown hint from a library error, if any."""
    response = _find_http_response(error)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    mapping = cast("Mapping[str, object]", headers)
    retry_after_header = mapping.get("Retry-After")
    if retry_after_header is None:
        retry_after_header = mapping.get("retry-after")
    return _retry_after_to_timedelta(retry_after_header)


def _classify_library_error(video_id: str, error: Exception) -> TranscriptFailure:
    """Map a `youtube-transcript-api` exception onto a provider failure value.

    An IP-block / rate-limit is the *blocked* outcome (carrying any retry-after
    hint); permanent media or transcript absence is *unavailable*; everything
    else is *transient* and retryable.
    """
    if _is_transcript_ip_block_error(error):
        return TranscriptBlockedFailure(
            message=f"youtube-transcript-api blocked for {video_id}: {error}",
            retry_after=_parse_retry_after(error),
            source=_SOURCE,
        )
    names = _mro_names(error)
    if names & _UNAVAILABLE_NAMES:
        return TranscriptUnavailableFailure(video_id=video_id)
    return TranscriptTransientFailure(
        f"youtube-transcript-api fetch for {video_id} failed: {error}"
    )


def _parse_snippets(raw: Iterable[Any]) -> str:
    """Join usable text from modern object or legacy mapping snippets."""
    snippets: list[str] = []
    for snippet in raw:
        if isinstance(snippet, Mapping):
            text = cast("Mapping[str, object]", snippet).get("text")
        else:
            text = getattr(snippet, "text", None)
        if isinstance(text, str) and (cleaned := text.strip()):
            snippets.append(cleaned)
    return " ".join(snippets)


def _default_library_fetcher(languages: tuple[str, ...]) -> Callable[[str], Any]:
    """Build the real fetcher: a `YouTubeTranscriptApi` instance, imported lazily.

    The library is an optional dependency; importing it here (not at module load)
    keeps Tether runnable without it and surfaces a clear error only when the
    fallback provider is actually wired.
    """
    try:
        module = importlib.import_module("youtube_transcript_api")
    except ImportError as error:  # pragma: no cover - exercised only without the dep
        message = (
            "youtube-transcript-api is not installed; install the 'youtube' "
            "dependency group to enable the transcript fallback provider"
        )
        raise TranscriptLibraryUnavailableError(message) from error
    api = module.YouTubeTranscriptApi()

    def _fetch(video_id: str) -> Any:
        return api.fetch(video_id, languages=list(languages))

    return _fetch


@dataclass(frozen=True, slots=True)
class TranscriptLibraryConfig:
    """Request pacing for the blocking transcript-library adapter."""

    min_request_interval: timedelta = _NO_PACING


class YouTubeTranscriptApiSource:
    """Fetch free YouTube transcripts through the blocking community library.

    Calls run off the event loop, are paced in process, and return typed source
    failures. Provider pauses and pass request limits remain in acquisition and
    worker orchestration rather than leaking into this leaf adapter.
    """

    @property
    def source(self) -> str:
        """The provenance tag for transcripts fetched through the free library."""
        return _SOURCE

    def __init__(
        self,
        fetcher: Callable[[str], Iterable[Any]] | None = None,
        *,
        languages: tuple[str, ...] = ("en",),
        config: TranscriptLibraryConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._fetcher: Callable[[str], Iterable[Any]] | None = fetcher
        self._languages: tuple[str, ...] = languages
        self._config: TranscriptLibraryConfig = config or TranscriptLibraryConfig()
        # Injectable so tests need not wait real seconds between paced calls.
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        # A monotonic clock (injectable for tests) drives the request-pacing gate;
        # the provider is long-lived, so it remembers the last call across fetches.
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._last_request_at: float | None = None

    def _ensure_fetcher(self) -> Callable[[str], Iterable[Any]]:
        if self._fetcher is None:
            self._fetcher = _default_library_fetcher(self._languages)
        return self._fetcher

    async def _throttle(self) -> None:
        """Wait out the min-interval since the previous real call, if configured.

        A no-op when pacing is disabled (zero interval, the default) or when the
        interval has already elapsed; the timestamp is stamped after any wait so
        it reflects the actual call time."""
        interval = self._config.min_request_interval.total_seconds()
        if interval <= 0:
            return
        if self._last_request_at is not None:
            wait = interval - (self._monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = self._monotonic()

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        """Fetch a transcript via the blocking library boundary."""
        fetcher = self._ensure_fetcher()
        await self._throttle()
        try:
            raw = await asyncio.to_thread(fetcher, video_id)
        except Exception as e:
            return Err(_classify_library_error(video_id, e))
        text = _parse_snippets(raw)
        if not text:
            return Err(TranscriptUnavailableFailure(video_id=video_id))
        return Ok(FetchedTranscript(text=text, source=_SOURCE))
