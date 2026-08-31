"""Behavior tests for OpenAI-compatible text-to-speech."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

import httpx2
from snekok.result import Err, Ok, Result
from snektest import assert_eq, assert_is_none, assert_raises, test

from tether.tts import TtsClient
from tether.tts_errors import (
    TtsConfigurationError,
    TtsFailure,
    TtsNetworkFailure,
    TtsRateLimitedFailure,
    TtsUpstreamFailure,
)
from tether.tts_model import SpeechResponse
from tether.tts_transport import HttpTtsTransport


@dataclass(frozen=True, slots=True)
class SynthesisCall:
    """One recorded provider synthesis invocation."""

    model: str
    response_format: str
    speed: float
    text: str
    voice: str


@dataclass
class FakeTtsTransport:
    """Return one outcome and record provider requests."""

    outcome: Result[SpeechResponse, TtsFailure]
    calls: list[SynthesisCall] = field(default_factory=list[SynthesisCall])

    async def synthesize(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        response_format: str,
        speed: float,
    ) -> Result[SpeechResponse, TtsFailure]:
        """Record the call and return the configured outcome."""
        self.calls.append(
            SynthesisCall(
                model=model,
                response_format=response_format,
                speed=speed,
                text=text,
                voice=voice,
            )
        )
        return self.outcome


def successful_response() -> SpeechResponse:
    """Return a small successful MP3 provider response."""
    return SpeechResponse(
        audio=b"speech-audio", content_type="audio/mpeg", status_code=200
    )


@test()
async def client_sends_configured_speech_settings() -> None:
    """Each synthesis uses configured model, voice, speed, and MP3 format."""
    transport = FakeTtsTransport(Ok(successful_response()))
    client = TtsClient(
        transport=transport,
        model="gpt-4o-mini-tts",
        speed=1.3,
        voice="cedar",
    )

    _ = await client.synthesize("Hello there.")

    assert_eq(
        transport.calls,
        [
            SynthesisCall(
                model="gpt-4o-mini-tts",
                response_format="mp3",
                speed=1.3,
                text="Hello there.",
                voice="cedar",
            )
        ],
    )


@test()
async def client_returns_generated_audio() -> None:
    """A successful provider response becomes playable speech."""
    client = TtsClient(
        transport=FakeTtsTransport(Ok(successful_response())),
        model="speech-model",
        voice="voice",
    )

    outcome = await client.synthesize("Hello.")

    assert isinstance(outcome, Ok)
    assert_eq(outcome.value.audio, b"speech-audio")
    assert_eq(outcome.value.content_type, "audio/mpeg")


@test()
async def client_returns_rate_limit_with_retry_hint() -> None:
    """A provider 429 remains a typed pacing failure."""
    client = TtsClient(
        transport=FakeTtsTransport(
            Ok(
                SpeechResponse(
                    audio=b"",
                    content_type="application/json",
                    retry_after=timedelta(seconds=9),
                    status_code=429,
                )
            )
        ),
        model="speech-model",
        voice="voice",
    )

    outcome = await client.synthesize("Hello.")

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, TtsRateLimitedFailure)
    assert_eq(outcome.error.retry_after, timedelta(seconds=9))


@test()
async def client_returns_upstream_failure() -> None:
    """A non-success provider response stays in the expected failure channel."""
    client = TtsClient(
        transport=FakeTtsTransport(
            Ok(
                SpeechResponse(
                    audio=b"",
                    content_type="application/json",
                    status_code=500,
                )
            )
        ),
        model="speech-model",
        voice="voice",
    )

    outcome = await client.synthesize("Hello.")

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, TtsUpstreamFailure)
    assert_eq(outcome.error.status_code, 500)
    assert_is_none(outcome.error.retry_after)


@test()
async def http_transport_posts_openai_compatible_request() -> None:
    """The HTTP adapter posts authenticated JSON to `/audio/speech`."""
    requests: list[httpx2.Request] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, content=b"mp3", headers={"content-type": "audio/mpeg"}
        )

    transport = HttpTtsTransport(
        "secret",
        base_url="https://speech.example/v1",
        http_transport=httpx2.MockTransport(capture),
    )

    outcome = await transport.synthesize(
        text="Read this.",
        model="speech-model",
        voice="cedar",
        response_format="mp3",
        speed=1.3,
    )

    assert isinstance(outcome, Ok)
    assert_eq(str(requests[0].url), "https://speech.example/v1/audio/speech")
    assert_eq(requests[0].headers["authorization"], "Bearer secret")
    assert_eq(
        json.loads(requests[0].content),
        {
            "input": "Read this.",
            "model": "speech-model",
            "response_format": "mp3",
            "speed": 1.3,
            "voice": "cedar",
        },
    )


@test()
async def http_transport_returns_network_failure() -> None:
    """A request failure does not escape the provider interface."""

    def disconnect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    transport = HttpTtsTransport(
        "secret", http_transport=httpx2.MockTransport(disconnect)
    )

    outcome = await transport.synthesize(
        text="Hello.",
        model="model",
        voice="voice",
        response_format="mp3",
        speed=1.0,
    )

    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, TtsNetworkFailure)
    assert_eq(outcome.error.reason, "offline")


@test()
def http_transport_requires_api_key() -> None:
    """Missing required provider credentials fail during host composition."""
    with assert_raises(TtsConfigurationError):
        _ = HttpTtsTransport("")
