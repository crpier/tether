"""OpenAI-compatible speech-to-text transcription for voice capture.

A single-user transcription capability against any OpenAI-compatible endpoint
(`POST <base_url>/audio/transcriptions`, multipart `file` + `model`, bearer
auth) — OpenAI, Groq, or a self-hosted server, chosen purely by base URL. Two
seams mirror the ingestion gates:

- `SttTransport` — the isolated HTTP boundary (one multipart POST), faked in
  tests so no live transcription call runs.
- `SttClient` — the thin policy above it: it passes the configured model with
  the upload and turns a non-success upstream status into a raised `SttError`
  carrying the status and any `Retry-After` hint. It never retries — a rate
  limit or upstream failure surfaces to the caller rather than looping.

>>> client = SttClient(transport=transport, model="whisper-1")
>>> await client.transcribe(audio)
'buy oat milk'
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, cast

import httpx2

_TRANSCRIPTIONS_PATH = "/audio/transcriptions"
_RATE_LIMITED_STATUS = 429
_SUCCESS_STATUS_RANGE = range(200, 300)
"""HTTP statuses read as a successful transcription; anything else surfaces."""
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT = timedelta(seconds=60)
"""Per-request HTTP timeout: transcription of a short voice note is slower than
a JSON call but bounded, so a minute is ample without hanging a stuck request."""


class SttConfigurationError(Exception):
    """Raised when the STT HTTP transport is built without an API key."""


class SttError(Exception):
    """Raised when a transcription request fails upstream.

    Carries the upstream `status_code` and, on a rate limit, the `retry_after`
    hint parsed from the response so the caller can surface a precise error
    (a 503 with a clear body) rather than a bare failure.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code: int = status_code
        self.retry_after: timedelta | None = retry_after


@dataclass(frozen=True, slots=True)
class AudioUpload:
    """One audio payload to transcribe, held only for the length of the request.

    The bytes are the recorded note itself; `filename` and `content_type` are
    forwarded to the multipart part so the upstream API can sniff the container
    (m4a/ogg/wav). The audio is not persisted — it exists only to be sent.
    """

    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class TranscriptionResponse:
    """One transcription HTTP response, normalized for the pure client logic.

    `text` is the recognized transcript (empty on a non-2xx); `retry_after` is
    any parsed `Retry-After` hint, meaningful only on a 429. Keeping the
    transport's output this small is what lets the client be unit-tested with a
    scripted fake instead of httpx.
    """

    status_code: int
    text: str
    retry_after: timedelta | None = None


class SttTransport(Protocol):
    """The isolated transcription HTTP boundary the client drives.

    One call: `transcribe` posts the audio and model, returning a normalized
    `TranscriptionResponse`. Faked in tests so the client's success/failure
    policy runs offline.
    """

    async def transcribe(
        self, *, audio: AudioUpload, model: str
    ) -> TranscriptionResponse:
        """Post one audio upload for transcription with the given model."""
        ...


class SttClient:
    """Transcription policy over an `SttTransport`.

    Sends the configured model with each upload and returns the recognized
    text on success. A non-success upstream status raises `SttError` — a 429
    surfacing its `Retry-After`, any other failure its status. There is
    deliberately no retry loop: a rate limit or outage is surfaced, not
    silently re-attempted.

    >>> client = SttClient(transport=transport, model="whisper-1")
    >>> await client.transcribe(audio)
    'buy oat milk'
    """

    def __init__(self, transport: SttTransport, *, model: str) -> None:
        self.transport: SttTransport = transport
        self.model: str = model

    async def transcribe(self, audio: AudioUpload) -> str:
        """Transcribe one audio upload to text, raising `SttError` on failure."""
        response = await self.transport.transcribe(audio=audio, model=self.model)
        if response.status_code == _RATE_LIMITED_STATUS:
            rate_limited_message = "transcription rate limited"
            raise SttError(
                rate_limited_message,
                status_code=response.status_code,
                retry_after=response.retry_after,
            )
        if response.status_code not in _SUCCESS_STATUS_RANGE:
            failure_message = f"transcription failed with status {response.status_code}"
            raise SttError(failure_message, status_code=response.status_code)
        return response.text


class HttpSttTransport(SttTransport):
    """The production `SttTransport`: a thin httpx multipart POST.

    Holds the API key and base URL and performs the one transcription POST,
    normalizing the response into a `TranscriptionResponse`. All success and
    retry policy lives above it in `SttClient`, keeping this boundary dumb and
    faked-in-tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: timedelta | None = None,
    ) -> None:
        if not api_key:
            message = "STT API key is required to build the HTTP transport"
            raise SttConfigurationError(message)
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._timeout: timedelta = timeout or _DEFAULT_TIMEOUT

    async def transcribe(
        self, *, audio: AudioUpload, model: str
    ) -> TranscriptionResponse:
        """Post the audio as multipart `file` + `model` with bearer auth."""
        async with httpx2.AsyncClient(
            base_url=self._base_url, timeout=self._timeout.total_seconds()
        ) as client:
            response = await client.post(
                _TRANSCRIPTIONS_PATH,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (audio.filename, audio.content, audio.content_type)},
                data={"model": model},
            )
        return _from_httpx(response)


def _from_httpx(response: Any) -> TranscriptionResponse:
    """Normalize an httpx response into a `TranscriptionResponse`.

    OpenAI-compatible transcription returns `{"text": "..."}` on success; the
    text is read best-effort so a non-JSON error body degrades to an empty
    transcript with its status preserved for the client to reject.
    """
    try:
        body = response.json()
    except Exception:
        body = {}
    payload: Mapping[str, object] = (
        cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}
    )
    text = payload.get("text")
    return TranscriptionResponse(
        status_code=int(response.status_code),
        text=text if isinstance(text, str) else "",
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


__all__ = [
    "AudioUpload",
    "HttpSttTransport",
    "SttClient",
    "SttConfigurationError",
    "SttError",
    "SttTransport",
    "TranscriptionResponse",
]
