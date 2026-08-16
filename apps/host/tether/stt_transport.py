"""OpenAI-compatible HTTP transport for speech-to-text transcription."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol, cast

import httpx2
from snekok import Err, Ok, Result

from tether.stt_errors import SttConfigurationError, SttFailure, SttNetworkFailure
from tether.stt_model import AudioUpload, TranscriptionResponse

_TRANSCRIPTIONS_PATH = "/audio/transcriptions"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT = timedelta(seconds=60)


class SttTransport(Protocol):
    """Structural boundary to one configured transcription provider."""

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str
    ) -> Result[TranscriptionResponse, SttFailure]:
        """Submit one audio upload and return its normalized provider outcome."""
        ...


class HttpSttTransport:
    """Submit multipart transcription requests to an OpenAI-compatible API.

    ```python
    transport = HttpSttTransport("secret")
    assert transport is not None
    ```
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: timedelta | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            message = "STT API key is required to build the HTTP transport"
            raise SttConfigurationError(message)
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._http_transport: httpx2.AsyncBaseTransport | None = http_transport
        self._timeout: timedelta = timeout or _DEFAULT_TIMEOUT

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str
    ) -> Result[TranscriptionResponse, SttFailure]:
        """Post one multipart audio request, translating known network failures."""
        try:
            async with httpx2.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout.total_seconds(),
                transport=self._http_transport,
            ) as client:
                response = await client.post(
                    _TRANSCRIPTIONS_PATH,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (audio.filename, audio.content, audio.content_type)},
                    data={"model": model, "prompt": prompt},
                )
        except httpx2.RequestError as error:
            return Err(SttNetworkFailure(reason=str(error)))
        return Ok(_from_httpx(response))


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` response header when present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    return None


def _from_httpx(response: httpx2.Response) -> TranscriptionResponse:
    """Normalize the provider response while preserving its status and pacing hint."""
    try:
        decoded_body: object = response.json()
    except json.JSONDecodeError:
        decoded_body = {}
    payload: Mapping[str, object]
    if isinstance(decoded_body, Mapping):
        payload = cast("Mapping[str, object]", decoded_body)
    else:
        payload = {}
    text = payload.get("text")
    return TranscriptionResponse(
        status_code=response.status_code,
        text=text if isinstance(text, str) else "",
        retry_after=_retry_after_seconds(response.headers),
    )
