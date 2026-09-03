"""Typed contracts and composition for transcript acquisition."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NewType, Protocol, runtime_checkable

from snekok.result import Err, Ok, Result
from snekok.types import (
    NonBlankStr,
    NonNegativeInt,
    StrictFrozenModel,
)

TranscriptionKey = NewType("TranscriptionKey", str)
"""Stable source-independent identity of one Transcription."""


@dataclass(frozen=True, slots=True)
class TranscriptionTarget:
    """Stable Transcription identity plus the locator understood by providers."""

    key: TranscriptionKey
    locator: str


@dataclass(frozen=True, slots=True)
class FetchedTranscript:
    """Usable transcript text, exact timed segments, and producing source."""

    source: str
    text: str
    segments: tuple[TranscriptSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptBlockedFailure:
    """An upstream block that should pause one source."""

    source: str
    message: str = ""
    retry_after: timedelta | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class TranscriptDeferredFailure:
    """Local pass policy deferred work without changing provider health."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class TranscriptTransientFailure:
    """A retryable failure for one Transcription target."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class TranscriptUnavailableFailure:
    """Every attempted source lacked a usable Transcript for one target."""

    locator: str

    def __str__(self) -> str:
        return self.locator


type TranscriptFailure = (
    TranscriptBlockedFailure
    | TranscriptDeferredFailure
    | TranscriptTransientFailure
    | TranscriptUnavailableFailure
)
type TranscriptFetchResult = Result[FetchedTranscript, TranscriptFailure]


class TranscriptSource(Protocol):
    """One upstream transcript source with no orchestration policy."""

    @property
    def source(self) -> str:
        """Stable source identity used for provenance and provider pauses."""
        ...

    async def fetch(self, locator: str, /) -> TranscriptFetchResult:
        """Fetch one Transcript or return an expected source failure."""
        ...


def _empty_source_set() -> frozenset[str]:
    return frozenset[str]()


def _empty_limits() -> dict[str, int]:
    return {}


@dataclass(slots=True)
class TranscriptFetchPolicy:
    """Mutable policy for one explicit acquisition pass.

    Deferred sources may become available in a later pass. Excluded sources are
    inapplicable to this video. Request limits are local safeguards and therefore
    produce deferral rather than an upstream block.
    """

    deferred_sources: frozenset[str] = field(default_factory=_empty_source_set)
    excluded_sources: frozenset[str] = field(default_factory=_empty_source_set)
    request_limits: Mapping[str, int] = field(default_factory=_empty_limits)
    attempts: dict[str, int] = field(default_factory=_empty_limits)

    def can_attempt(self, source: str) -> bool:
        """Whether this pass may make another request to `source`."""
        limit = self.request_limits.get(source)
        return limit is None or self.attempts.get(source, 0) < limit

    def record_attempt(self, source: str) -> None:
        """Record one real source request against this pass's local limit."""
        self.attempts[source] = self.attempts.get(source, 0) + 1


@runtime_checkable
class AsyncClosable(Protocol):
    """An object owning async resources."""

    async def aclose(self) -> None:
        """Release owned resources."""
        ...


class TranscriptProviderConfigurationError(Exception):
    """Raised when provider composition violates a startup invariant."""


class TranscriptProviderChain:
    """Try configured sources in order while keeping policy outside leaves."""

    def __init__(self, sources: Sequence[TranscriptSource]) -> None:
        if not sources:
            message = "a transcript provider chain requires at least one source"
            raise TranscriptProviderConfigurationError(message)
        self._sources: tuple[TranscriptSource, ...] = tuple(sources)

    @property
    def sources(self) -> tuple[TranscriptSource, ...]:
        """Configured sources in attempt order."""
        return self._sources

    async def fetch(
        self,
        locator: str,
        *,
        policy: TranscriptFetchPolicy | None = None,
    ) -> TranscriptFetchResult:
        """Try eligible sources until one succeeds or returns a hard failure."""
        selected_policy = policy or TranscriptFetchPolicy()
        deferred = False
        last_unavailable: TranscriptUnavailableFailure | None = None
        for source in self._sources:
            if source.source in selected_policy.excluded_sources:
                continue
            if (
                source.source in selected_policy.deferred_sources
                or not selected_policy.can_attempt(source.source)
            ):
                deferred = True
                continue
            selected_policy.record_attempt(source.source)
            outcome = await source.fetch(locator)
            if isinstance(outcome, Ok):
                return outcome
            failure = outcome.unwrap_error()
            if isinstance(failure, TranscriptUnavailableFailure):
                last_unavailable = failure
            else:
                return outcome
        if deferred:
            return Err(
                TranscriptDeferredFailure(
                    message=f"transcript acquisition deferred for {locator}"
                )
            )
        return Err(last_unavailable or TranscriptUnavailableFailure(locator=locator))

    async def aclose(self) -> None:
        """Close resources owned by configured sources."""
        for source in reversed(self._sources):
            if isinstance(source, AsyncClosable):
                await source.aclose()


@dataclass(frozen=True, slots=True)
class TranscriptStored:
    """Acquisition resolved to usable stored transcript text."""

    cached: bool
    source: str | None
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptNeedsReview:
    """Every eligible source reported permanent unavailability."""

    target: TranscriptionTarget


@dataclass(frozen=True, slots=True)
class TranscriptRetryScheduled:
    """A transient failure scheduled another attempt for the target."""

    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptProviderBlocked:
    """A source was paused after an upstream block."""

    paused_until: datetime
    source: str


@dataclass(frozen=True, slots=True)
class TranscriptAcquisitionDeferred:
    """Local or persisted policy deferred acquisition to a later pass."""

    target: TranscriptionTarget


type TranscriptAcquisitionOutcome = (
    TranscriptStored
    | TranscriptNeedsReview
    | TranscriptRetryScheduled
    | TranscriptProviderBlocked
    | TranscriptAcquisitionDeferred
)


@dataclass(frozen=True, slots=True)
class TranscriptExplicitlyUnavailable:
    """The human has settled transcript absence for one Transcription."""

    target: TranscriptionTarget


type TranscriptRequestFailure = (
    TranscriptNeedsReview
    | TranscriptRetryScheduled
    | TranscriptProviderBlocked
    | TranscriptAcquisitionDeferred
    | TranscriptExplicitlyUnavailable
)


class TranscriptAcquisitionPort(Protocol):
    """Shared acquisition capability used by background and on-demand paths."""

    async def acquire(
        self,
        target: TranscriptionTarget,
        *,
        now: datetime,
        policy: TranscriptFetchPolicy | None = None,
    ) -> TranscriptAcquisitionOutcome:
        """Fetch and persist one transcript according to explicit pass policy."""
        ...


class TranscriptSegment(StrictFrozenModel):
    """One exact provider-reported timed transcript segment."""

    text: NonBlankStr
    start_ms: NonNegativeInt
    duration_ms: NonNegativeInt

    @property
    def end_ms(self) -> int:
        """Return the exclusive segment end in integer milliseconds."""
        return self.start_ms + self.duration_ms
