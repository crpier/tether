"""Text-to-speech policy over a structural provider transport."""

from snekok.result import Err, Ok, Result

from tether.tts_errors import (
    TtsFailure,
    TtsRateLimitedFailure,
    TtsUpstreamFailure,
)
from tether.tts_model import GeneratedSpeech
from tether.tts_transport import TtsTransport

_RATE_LIMITED_STATUS = 429
_SUCCESS_STATUS_RANGE = range(200, 300)
_DEFAULT_RESPONSE_FORMAT = "mp3"


class TtsClient:
    """Generate playable speech through one configured provider.

    ```python
    client = TtsClient(transport=transport, model="gpt-4o-mini-tts", voice="alloy")
    ```
    """

    def __init__(
        self,
        transport: TtsTransport,
        *,
        model: str,
        voice: str,
        response_format: str = _DEFAULT_RESPONSE_FORMAT,
        speed: float = 1.0,
    ) -> None:
        self._model: str = model
        self._response_format: str = response_format
        self._speed: float = speed
        self._transport: TtsTransport = transport
        self._voice: str = voice

    async def synthesize(self, text: str) -> Result[GeneratedSpeech, TtsFailure]:
        """Return generated audio or a typed expected provider failure."""
        outcome = await self._transport.synthesize(
            text=text,
            model=self._model,
            voice=self._voice,
            response_format=self._response_format,
            speed=self._speed,
        )
        if isinstance(outcome, Err):
            return Err(outcome.error)
        response = outcome.unwrap()
        if response.status_code == _RATE_LIMITED_STATUS:
            return Err(
                TtsRateLimitedFailure(
                    retry_after=response.retry_after,
                    status_code=response.status_code,
                )
            )
        if response.status_code not in _SUCCESS_STATUS_RANGE:
            return Err(
                TtsUpstreamFailure(
                    retry_after=response.retry_after,
                    status_code=response.status_code,
                )
            )
        return Ok(
            GeneratedSpeech(audio=response.audio, content_type=response.content_type)
        )
