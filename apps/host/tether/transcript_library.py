"""The `youtube-transcript-api` fallback `TranscriptProvider`.

The captions API (`CaptionsTranscriptProvider`) only produces transcripts for a
subset of videos; the community `youtube-transcript-api` library has much wider
coverage but gets the host IP-blocked when used too aggressively. This module
wraps that library behind the `TranscriptProvider` port so the composite
(`FallbackTranscriptProvider`) can try it second, and isolates the two pieces of
resilience logic the worker's global pause depends on:

* `_is_transcript_ip_block_error` — classify an IP-block / rate-limit response as
  the distinct *blocked* outcome (so the worker pauses the whole provider rather
  than retrying per-video).
* `_parse_retry_after` — extract any `Retry-After` hint so the cooldown honors the
  provider's guidance.

Both are pure functions over exception shapes, unit-tested without ever importing
the real library or touching a socket. The real library is imported lazily so the
rest of Tether runs without it installed.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast

from tether.youtube import (
    FetchedTranscript,
    TranscriptBlockedError,
    TranscriptExcludedError,
    TranscriptProvider,
    TranscriptSegment,
    TranscriptTransientError,
    TranscriptUnavailableError,
)

_SOURCE = "youtube_transcript_api"


class TranscriptLibraryUnavailableError(Exception):
    """The optional `youtube-transcript-api` dependency is not installed."""


# The library's exception class *names* (matched across the MRO so subclasses
# count) mapped onto the typed `TranscriptProvider` outcomes. Names rather than
# imported types so classification is unit-testable against stand-in exceptions
# without importing the real library.
_IP_BLOCK_NAMES = frozenset({"RequestBlocked", "IpBlocked"})
_UNAVAILABLE_NAMES = frozenset(
    {
        "TranscriptsDisabled",
        "NoTranscriptFound",
        "VideoUnavailable",
        "InvalidVideoId",
        "YouTubeDataUnparsable",
    }
)
_EXCLUDED_NAMES = frozenset({"AgeRestricted", "VideoUnplayable"})

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


def _classify_library_error(video_id: str, error: Exception) -> Exception:
    """Map a `youtube-transcript-api` failure onto a typed `TranscriptProvider` signal.

    An IP-block / rate-limit is the *blocked* outcome (carrying any retry-after
    hint); transcripts-disabled / not-found / gone is *unavailable* (terminal);
    age-restricted / unplayable is *excluded* (terminal + purged); everything else
    — transport errors, request failures — is *transient* and retryable.
    """
    if _is_transcript_ip_block_error(error):
        return TranscriptBlockedError(
            f"youtube-transcript-api blocked for {video_id}: {error}",
            retry_after=_parse_retry_after(error),
        )
    names = _mro_names(error)
    if names & _UNAVAILABLE_NAMES:
        return TranscriptUnavailableError(video_id)
    if names & _EXCLUDED_NAMES:
        return TranscriptExcludedError(video_id)
    return TranscriptTransientError(
        f"youtube-transcript-api fetch for {video_id} failed: {error}"
    )


def _parse_snippets(raw: Iterable[Any]) -> tuple[str, tuple[TranscriptSegment, ...]]:
    """Parse the library's transcript snippets into joined text plus timed segments.

    Accepts both the modern object snippets (`.text` / `.start`) and the legacy
    dict snippets (`{"text", "start"}`). Empty or untimed cues are dropped; the
    joined text is what keyword Search matches.
    """
    segments: list[TranscriptSegment] = []
    for snippet in raw:
        text: object
        start: object
        if isinstance(snippet, Mapping):
            mapping = cast("Mapping[str, object]", snippet)
            text = mapping.get("text")
            start = mapping.get("start")
        else:
            text = getattr(snippet, "text", None)
            start = getattr(snippet, "start", None)
        if not isinstance(text, str):
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        start_seconds = float(start) if isinstance(start, (int, float)) else 0.0
        segments.append(TranscriptSegment(start_seconds=start_seconds, text=cleaned))
    joined = " ".join(segment.text for segment in segments)
    return joined, tuple(segments)


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


class YouTubeTranscriptApiProvider(TranscriptProvider):
    """The fallback `TranscriptProvider` backed by `youtube-transcript-api`.

    Wider coverage than the captions API but IP-block-prone, so it is composed
    *behind* the captions provider in `FallbackTranscriptProvider` and skipped
    while the worker's global pause is in effect. The blocking library call runs in
    a worker thread; failures are classified into the typed outcomes (notably the
    distinct *blocked* signal that trips the global pause). It holds no budget — the
    worker and on-demand path charge the daily budget before calling `fetch`.
    """

    def __init__(
        self,
        fetcher: Callable[[str], Iterable[Any]] | None = None,
        *,
        languages: tuple[str, ...] = ("en",),
    ) -> None:
        self._fetcher: Callable[[str], Iterable[Any]] | None = fetcher
        self._languages: tuple[str, ...] = languages

    def _ensure_fetcher(self) -> Callable[[str], Iterable[Any]]:
        if self._fetcher is None:
            self._fetcher = _default_library_fetcher(self._languages)
        return self._fetcher

    async def fetch(
        self, video_id: str, *, skip_blockable: bool = False
    ) -> FetchedTranscript:
        """Fetch a transcript via the library, or raise a typed signal.

        This *is* the blockable source, so `skip_blockable` does not apply here —
        the composite provider is what skips it while the worker is paused.
        """
        _ = skip_blockable
        fetcher = self._ensure_fetcher()
        try:
            raw = await asyncio.to_thread(fetcher, video_id)
        except Exception as error:
            raise _classify_library_error(video_id, error) from error
        text, segments = _parse_snippets(raw)
        if not text:
            raise TranscriptUnavailableError(video_id)
        return FetchedTranscript(text=text, segments=segments, source=_SOURCE)
