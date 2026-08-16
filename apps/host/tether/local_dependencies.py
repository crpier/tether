"""Deterministic local adapters for boundaries that normally leave the host."""

from __future__ import annotations

from collections.abc import Callable

from snekok import Ok, Result

from tether.provider_auth import ProviderAuthBackend
from tether.provider_auth_errors import ProviderAuthFailure
from tether.provider_auth_model import DeviceCode
from tether.stt import AudioUpload, SttTransport, TranscriptionResponse


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


class LocalSttTransport(SttTransport):
    """Return stable text without uploading recorded audio."""

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str
    ) -> TranscriptionResponse:
        """Transcribe every local recording into one recognizable fixture phrase."""
        _ = (audio, model, prompt)
        return TranscriptionResponse(status_code=200, text="Local transcription.")
