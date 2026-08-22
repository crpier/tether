"""Typed text-to-speech failures, separate from HTTP presentation."""

from dataclasses import dataclass
from datetime import timedelta


class TtsConfigurationError(Exception):
    """The required text-to-speech provider configuration is invalid."""


@dataclass(frozen=True, slots=True)
class TtsNetworkFailure:
    """The configured speech provider could not be reached."""

    reason: str


@dataclass(frozen=True, slots=True)
class TtsRateLimitedFailure:
    """The speech provider rejected a request for exceeding its limit."""

    retry_after: timedelta | None
    status_code: int


@dataclass(frozen=True, slots=True)
class TtsUpstreamFailure:
    """The speech provider returned a non-success response."""

    retry_after: timedelta | None
    status_code: int


type TtsFailure = TtsNetworkFailure | TtsRateLimitedFailure | TtsUpstreamFailure
