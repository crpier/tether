"""Behavior tests for the OpenAI-compatible speech-to-text boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import httpx2
from snekok.result import Err, Ok, Result
from snektest import (
    assert_eq,
    assert_false,
    assert_is_none,
    assert_raises,
    assert_true,
    test,
)

from tether.stt import SttClient
from tether.stt_errors import (
    SttConfigurationError,
    SttFailure,
    SttNetworkFailure,
    SttRateLimitedFailure,
    SttUpstreamFailure,
)
from tether.stt_model import AudioUpload, TranscriptionResponse
from tether.stt_transport import HttpSttTransport


class TransportDefect(Exception):
    """Unexpected transport defect used to verify exception propagation."""


@dataclass
class TranscribeCall:
    """One recorded transcription invocation."""

    audio: AudioUpload
    model: str
    prompt: str
    language: str


@dataclass
class FakeSttTransport:
    """Return a scripted transport outcome and record each request."""

    outcome: Result[TranscriptionResponse, SttFailure]
    calls: list[TranscribeCall] = field(default_factory=list[TranscribeCall])

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str, language: str
    ) -> Result[TranscriptionResponse, SttFailure]:
        """Record the call and return the scripted outcome."""
        self.calls.append(
            TranscribeCall(audio=audio, model=model, prompt=prompt, language=language)
        )
        return self.outcome


class DefectiveSttTransport:
    """Raise an unexpected defect instead of returning an expected failure."""

    async def transcribe(
        self, *, audio: AudioUpload, model: str, prompt: str, language: str
    ) -> Result[TranscriptionResponse, SttFailure]:
        """Fail outside the typed expected-failure channel."""
        _ = (audio, model, prompt, language)
        raise TransportDefect


def _audio() -> AudioUpload:
    """Build a tiny stand-in audio upload."""
    return AudioUpload(
        content=b"fake-audio", filename="note.m4a", content_type="audio/mp4"
    )


@test()
async def client_returns_transcribed_text_on_success() -> None:
    """A successful upstream response yields recognized text."""
    transport = FakeSttTransport(
        Ok(TranscriptionResponse(status_code=200, text="buy oat milk"))
    )
    client = SttClient(transport=transport, model="whisper-1")

    outcome = await client.transcribe(_audio())

    assert isinstance(outcome, Ok)
    assert_eq(outcome.value, "buy oat milk")


@test()
async def client_passes_the_configured_model_to_the_transport() -> None:
    """The client sends its configured model with the upload."""
    transport = FakeSttTransport(Ok(TranscriptionResponse(status_code=200, text="hi")))
    client = SttClient(transport=transport, model="whisper-large-v3")

    _ = await client.transcribe(_audio())

    assert_eq(transport.calls[0].model, "whisper-large-v3")


@test()
async def client_pins_the_configured_language_on_every_request() -> None:
    """Every transcription carries the pinned language so detection never runs."""
    transport = FakeSttTransport(Ok(TranscriptionResponse(status_code=200, text="hi")))
    client = SttClient(transport=transport, model="whisper-1", language="en")

    _ = await client.transcribe(_audio())

    assert_eq(transport.calls[0].language, "en")


@test()
async def client_sends_no_prompt_until_a_glossary_is_configured() -> None:
    """Without a configured glossary the vocabulary prompt stays empty."""
    transport = FakeSttTransport(Ok(TranscriptionResponse(status_code=200, text="hi")))
    client = SttClient(transport=transport, model="whisper-1")

    _ = await client.transcribe(_audio())

    assert_eq(transport.calls[0].prompt, "")


@test()
async def client_carries_a_configured_glossary_prompt_through() -> None:
    """A configured glossary rides every transcription as the STT prompt."""
    transport = FakeSttTransport(Ok(TranscriptionResponse(status_code=200, text="hi")))
    client = SttClient(
        transport=transport, model="whisper-1", prompt="Tether, snekok, pi"
    )

    _ = await client.transcribe(_audio())

    assert_eq(transport.calls[0].prompt, "Tether, snekok, pi")


@test()
async def rate_limit_is_a_typed_failure_with_retry_after() -> None:
    """A 429 is returned as data with its `Retry-After` hint."""
    transport = FakeSttTransport(
        Ok(
            TranscriptionResponse(
                status_code=429, text="", retry_after=timedelta(seconds=12)
            )
        )
    )
    client = SttClient(transport=transport, model="whisper-1")

    outcome = await client.transcribe(_audio())

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, SttRateLimitedFailure)
    assert_eq(outcome.error.status_code, 429)
    assert_eq(outcome.error.retry_after, timedelta(seconds=12))


@test()
async def upstream_error_is_a_typed_failure() -> None:
    """A 5xx response remains in the expected-failure channel."""
    transport = FakeSttTransport(Ok(TranscriptionResponse(status_code=500, text="")))
    client = SttClient(transport=transport, model="whisper-1")

    outcome = await client.transcribe(_audio())

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, SttUpstreamFailure)
    assert_eq(outcome.error.status_code, 500)
    assert_is_none(outcome.error.retry_after)


@test()
async def unexpected_transport_defect_propagates() -> None:
    """Programmer and unknown transport defects remain exceptions."""
    client = SttClient(transport=DefectiveSttTransport(), model="whisper-1")

    with assert_raises(TransportDefect):
        _ = await client.transcribe(_audio())


@test()
async def http_transport_returns_network_failure_as_data() -> None:
    """A known request failure does not escape the provider boundary."""

    def disconnect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    transport = HttpSttTransport(
        "secret", http_transport=httpx2.MockTransport(disconnect)
    )

    outcome = await transport.transcribe(
        audio=_audio(), model="whisper-1", prompt="", language="en"
    )

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, SttNetworkFailure)
    assert_eq(outcome.error.reason, "offline")


@test()
async def http_transport_sends_language_and_omits_an_empty_prompt() -> None:
    """The multipart request pins `language` and carries no empty `prompt` field."""
    bodies: list[bytes] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(200, json={"text": "hi"})

    transport = HttpSttTransport("secret", http_transport=httpx2.MockTransport(capture))

    outcome = await transport.transcribe(
        audio=_audio(), model="whisper-1", prompt="", language="en"
    )

    assert isinstance(outcome, Ok)
    body = bodies[0]
    assert_true(b'name="model"' in body)
    assert_true(b'name="language"\r\n\r\nen' in body)
    assert_false(b'name="prompt"' in body)


@test()
async def http_transport_sends_a_configured_prompt() -> None:
    """A non-empty glossary prompt is sent verbatim as the `prompt` field."""
    bodies: list[bytes] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(200, json={"text": "hi"})

    transport = HttpSttTransport("secret", http_transport=httpx2.MockTransport(capture))

    outcome = await transport.transcribe(
        audio=_audio(), model="whisper-1", prompt="Tether", language="en"
    )

    assert isinstance(outcome, Ok)
    assert_true(b'name="prompt"\r\n\r\nTether' in bodies[0])


@test()
def http_transport_requires_an_api_key() -> None:
    """Building the HTTP transport without a key is a configuration error."""
    with assert_raises(SttConfigurationError):
        _ = HttpSttTransport("")
