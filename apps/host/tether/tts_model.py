"""Text-to-speech provider and playback values."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class GeneratedSpeech:
    """One ephemeral audio response ready for browser playback."""

    audio: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class SpeechResponse:
    """Normalized response from an OpenAI-compatible speech provider."""

    audio: bytes
    content_type: str
    status_code: int
    retry_after: timedelta | None = None
