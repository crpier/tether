"""Typed speech-to-text failures, separate from their HTTP presentation."""

from dataclasses import dataclass
from datetime import timedelta


class SttConfigurationError(Exception):
    """The required speech-to-text provider configuration is invalid."""


@dataclass(frozen=True, slots=True)
class AudioUploadMalformedFailure:
    """The request body is not a valid multipart upload."""


@dataclass(frozen=True, slots=True)
class AudioUploadMissingFailure:
    """The multipart request does not contain an audio file part."""


@dataclass(frozen=True, slots=True)
class AudioUploadTooLargeFailure:
    """The uploaded audio exceeds the configured provider ceiling."""

    maximum_megabytes: int


@dataclass(frozen=True, slots=True)
class SttNetworkFailure:
    """The configured transcription provider could not be reached."""

    reason: str


@dataclass(frozen=True, slots=True)
class SttRateLimitedFailure:
    """The transcription provider rejected a request for exceeding its limit."""

    retry_after: timedelta | None
    status_code: int


@dataclass(frozen=True, slots=True)
class SttUpstreamFailure:
    """The transcription provider returned a non-success response."""

    retry_after: timedelta | None
    status_code: int


type AudioUploadFailure = (
    AudioUploadMalformedFailure | AudioUploadMissingFailure | AudioUploadTooLargeFailure
)
type SttFailure = SttNetworkFailure | SttRateLimitedFailure | SttUpstreamFailure
