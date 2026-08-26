"""Speech-to-text request and provider response values."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class AudioUpload:
    """One ephemeral audio payload sent for transcription.

    ```python
    upload = AudioUpload(content=b"audio", content_type="audio/wav", filename="a.wav")
    assert upload.filename == "a.wav"
    ```
    """

    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class TranscriptionResponse:
    """Normalized response from an OpenAI-compatible transcription provider."""

    status_code: int
    text: str
    retry_after: timedelta | None = None
