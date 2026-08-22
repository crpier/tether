"""Deterministic local adapters for boundaries that normally leave the host."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Callable

from snekok import Ok, Result

from tether.provider_auth import ProviderAuthBackend
from tether.provider_auth_errors import ProviderAuthFailure
from tether.provider_auth_model import DeviceCode
from tether.stt_errors import SttFailure
from tether.stt_model import AudioUpload, TranscriptionResponse
from tether.tts_errors import TtsFailure
from tether.tts_model import SpeechResponse
from tether.youtube import YouTubeAuthBackend, YouTubeAuthFailure, YouTubeAuthorization


class LocalProviderAuthBackend(ProviderAuthBackend):
    """Report the deterministic local model provider as always connected."""

    async def check(self) -> Result[bool, ProviderAuthFailure]:
        """Report local provider availability without reading credential state."""
        return Ok(value=True)

    async def authorize(
        self, report: Callable[[DeviceCode], None]
    ) -> Result[None, ProviderAuthFailure]:
        """Complete immediately because the local provider needs no credentials."""
        _ = report
        return Ok(None)


class LocalYouTubeAuthBackend(YouTubeAuthBackend):
    """Complete a deterministic same-origin Google consent round trip."""

    def __init__(self) -> None:
        self._connected: bool = False

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        """Report whether the local callback has completed."""
        return Ok(self._connected)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        """Return a callback URL that models successful Google consent."""
        return Ok(
            YouTubeAuthorization(
                authorization_url=(
                    f"{redirect_uri}?state=local-youtube-state&code=local-code"
                ),
                state="local-youtube-state",
            )
        )

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        """Remember successful consent for the local process lifetime."""
        _ = (authorization_response, expected_state)
        self._connected = True
        return Ok(None)


_LOCAL_SILENCE_WAV_BASE64 = (
    "UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)
"""A valid ten-millisecond WAV used by the deterministic local provider."""


class LocalTtsTransport:
    """Return stable audio without calling a speech provider."""

    async def synthesize(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        response_format: str,
        speed: float,
    ) -> Result[SpeechResponse, TtsFailure]:
        """Render every local fragment as a tiny deterministic MP3 fixture."""
        _ = (text, model, voice, response_format, speed)
        return Ok(
            SpeechResponse(
                audio=b64decode(_LOCAL_SILENCE_WAV_BASE64),
                content_type="audio/wav",
                status_code=200,
            )
        )


class LocalSttTransport:
    """Return stable text without uploading recorded audio."""

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str, language: str
    ) -> Result[TranscriptionResponse, SttFailure]:
        """Transcribe every local recording into one recognizable fixture phrase."""
        _ = (audio, model, prompt, language)
        return Ok(TranscriptionResponse(status_code=200, text="Local transcription."))
