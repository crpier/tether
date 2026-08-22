"""OpenAI-compatible HTTP transport for text-to-speech generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol

import httpx2
from snekok import Err, Ok, Result

from tether.tts_errors import TtsConfigurationError, TtsFailure, TtsNetworkFailure
from tether.tts_model import SpeechResponse

_SPEECH_PATH = "/audio/speech"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT = timedelta(seconds=60)
_CONTENT_TYPES = {
    "aac": "audio/aac",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "pcm": "audio/L16",
    "wav": "audio/wav",
}


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` response header when present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    return None


class TtsTransport(Protocol):
    """Structural interface to one configured speech provider."""

    async def synthesize(
        self, *, text: str, model: str, voice: str, response_format: str
    ) -> Result[SpeechResponse, TtsFailure]:
        """Submit text and return its normalized provider outcome."""
        ...


class HttpTtsTransport:
    """Submit speech requests to an OpenAI-compatible provider.

    ```python
    transport = HttpTtsTransport("secret")
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
            message = "TTS API key is required to build the HTTP transport"
            raise TtsConfigurationError(message)
        self._api_key: str = api_key
        self._base_url: str = base_url
        self._http_transport: httpx2.AsyncBaseTransport | None = http_transport
        self._timeout: timedelta = timeout or _DEFAULT_TIMEOUT

    async def synthesize(
        self, *, text: str, model: str, voice: str, response_format: str
    ) -> Result[SpeechResponse, TtsFailure]:
        """Post one speech request, translating known network failures."""
        try:
            async with httpx2.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout.total_seconds(),
                transport=self._http_transport,
            ) as client:
                response = await client.post(
                    _SPEECH_PATH,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "input": text,
                        "model": model,
                        "response_format": response_format,
                        "voice": voice,
                    },
                )
        except httpx2.RequestError as error:
            return Err(TtsNetworkFailure(reason=str(error)))
        return Ok(
            SpeechResponse(
                audio=response.content,
                content_type=response.headers.get(
                    "content-type", _CONTENT_TYPES.get(response_format, "audio/mpeg")
                ).split(";", maxsplit=1)[0],
                retry_after=_retry_after_seconds(response.headers),
                status_code=response.status_code,
            )
        )
