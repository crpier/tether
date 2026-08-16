"""Speech-to-text policy over a structural provider transport."""

from snekok import Err, Ok, Result

from tether.stt_errors import (
    SttFailure,
    SttRateLimitedFailure,
    SttUpstreamFailure,
)
from tether.stt_model import AudioUpload
from tether.stt_transport import SttTransport

_RATE_LIMITED_STATUS = 429
_SUCCESS_STATUS_RANGE = range(200, 300)
_STT_VOCABULARY_PROMPT = "snektest"


class SttClient:
    """Transcribe audio without hiding retries or expected provider failures.

    ```python
    client = SttClient(transport=transport, model="whisper-1")
    outcome = await client.transcribe(audio)
    ```
    """

    def __init__(self, transport: SttTransport, *, model: str) -> None:
        self._transport: SttTransport = transport
        self._model: str = model

    async def transcribe(self, audio: AudioUpload) -> Result[str, SttFailure]:
        """Return recognized text or a typed expected provider failure."""
        outcome = await self._transport.transcribe(
            audio=audio, model=self._model, prompt=_STT_VOCABULARY_PROMPT
        )
        if isinstance(outcome, Err):
            return Err(outcome.error)
        response = outcome.value
        if response.status_code == _RATE_LIMITED_STATUS:
            return Err(
                SttRateLimitedFailure(
                    retry_after=response.retry_after,
                    status_code=response.status_code,
                )
            )
        if response.status_code not in _SUCCESS_STATUS_RANGE:
            return Err(
                SttUpstreamFailure(
                    retry_after=response.retry_after,
                    status_code=response.status_code,
                )
            )
        return Ok(response.text)
