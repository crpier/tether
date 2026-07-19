"""Behaviour tests for the OpenAI-compatible speech-to-text capability.

These drive `SttClient` against a scripted `FakeSttTransport`, never a live
transcription API. They assert the two things the client owns above the HTTP
boundary: a successful transcription returns the recognized text, and a
non-success upstream status surfaces as a raised `SttError` carrying the status
and any `Retry-After` hint — no silent retry loop. The HTTP transport's own
key requirement is checked directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from snektest import assert_eq, assert_is_none, assert_raises, test

from tether.stt import (
    AudioUpload,
    HttpSttTransport,
    SttClient,
    SttConfigurationError,
    SttError,
    TranscriptionResponse,
)


@dataclass
class TranscribeCall:
    """One recorded `transcribe` invocation, for request-shape assertions."""

    audio: AudioUpload
    model: str


@dataclass
class FakeSttTransport:
    """A scripted `SttTransport`: returns a queued response, records the call.

    `response` is handed back verbatim from the single `transcribe` call a
    client makes, so a test scripts one upstream outcome (a 200 with text, a
    429 with a `Retry-After`, a 500) and inspects the recorded call.
    """

    response: TranscriptionResponse
    calls: list[TranscribeCall] = field(default_factory=list[TranscribeCall])

    async def transcribe(
        self, *, audio: AudioUpload, model: str
    ) -> TranscriptionResponse:
        """Record the call and return the scripted response."""
        self.calls.append(TranscribeCall(audio=audio, model=model))
        return self.response


def _audio() -> AudioUpload:
    """A tiny stand-in audio upload for the client tests."""
    return AudioUpload(
        content=b"fake-audio", filename="note.m4a", content_type="audio/mp4"
    )


@test()
async def client_returns_transcribed_text_on_success() -> None:
    """A 200 upstream response yields the recognized transcript text."""
    transport = FakeSttTransport(
        TranscriptionResponse(status_code=200, text="buy oat milk")
    )
    client = SttClient(transport=transport, model="whisper-1")

    transcript = await client.transcribe(_audio())

    assert_eq(transcript, "buy oat milk")


@test()
async def client_passes_the_configured_model_to_the_transport() -> None:
    """The client sends its configured model with the upload."""
    transport = FakeSttTransport(TranscriptionResponse(status_code=200, text="hi"))
    client = SttClient(transport=transport, model="whisper-large-v3")

    _ = await client.transcribe(_audio())

    assert_eq(transport.calls[0].model, "whisper-large-v3")


@test()
async def rate_limited_surfaces_error_with_retry_after() -> None:
    """A 429 raises `SttError` carrying the status and the `Retry-After` hint."""
    transport = FakeSttTransport(
        TranscriptionResponse(
            status_code=429, text="", retry_after=timedelta(seconds=12)
        )
    )
    client = SttClient(transport=transport, model="whisper-1")

    with assert_raises(SttError) as caught:
        _ = await client.transcribe(_audio())

    assert_eq(caught.exception.status_code, 429)
    assert_eq(caught.exception.retry_after, timedelta(seconds=12))


@test()
async def upstream_error_surfaces_as_stt_error() -> None:
    """A 5xx upstream status raises `SttError` rather than returning text."""
    transport = FakeSttTransport(TranscriptionResponse(status_code=500, text=""))
    client = SttClient(transport=transport, model="whisper-1")

    with assert_raises(SttError) as caught:
        _ = await client.transcribe(_audio())

    assert_eq(caught.exception.status_code, 500)
    assert_is_none(caught.exception.retry_after)


@test()
def http_transport_requires_an_api_key() -> None:
    """Building the HTTP transport without a key is a configuration error."""
    with assert_raises(SttConfigurationError):
        _ = HttpSttTransport("")
