"""Deterministic local adapters for boundaries that normally leave the host."""

from __future__ import annotations

from collections.abc import Callable

from tether.provider_auth import DeviceCode, ProviderAuthBackend
from tether.stt import AudioUpload, SttTransport, TranscriptionResponse


class LocalProviderAuthBackend(ProviderAuthBackend):
    """Report the deterministic local model provider as always connected."""

    async def check(self) -> bool:
        """Report local provider availability without reading credential state."""
        return True

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        """Complete immediately because the local provider needs no credentials."""
        _ = report


class LocalSttTransport(SttTransport):
    """Return stable text without uploading recorded audio."""

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str
    ) -> TranscriptionResponse:
        """Transcribe every local recording into one recognizable fixture phrase."""
        _ = (audio, model, prompt)
        return TranscriptionResponse(status_code=200, text="Local transcription.")
