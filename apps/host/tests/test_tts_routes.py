"""Behavior tests for provider-generated speech replies."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from snekok import Ok, Result
from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tts import TtsClient
from tether.tts_errors import TtsFailure
from tether.tts_model import SpeechResponse

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


class ScriptedTtsTransport:
    """Return one configured provider response."""

    def __init__(self, response: SpeechResponse) -> None:
        self.response: SpeechResponse = response

    async def synthesize(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        response_format: str,
        speed: float,
    ) -> Result[SpeechResponse, TtsFailure]:
        """Return the scripted speech response."""
        _ = (text, model, voice, response_format, speed)
        return Ok(self.response)


def make_client(root: Path, *, response: SpeechResponse) -> TestClient:
    """Create an app with deterministic provider speech."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                tts_client=TtsClient(
                    transport=ScriptedTtsTransport(response),
                    model="gpt-4o-mini-tts",
                    voice="alloy",
                ),
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


@test()
def speech_endpoint_returns_provider_audio() -> None:
    """A successful synthesis returns playable audio unchanged."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"provider-mp3",
                content_type="audio/mpeg",
                status_code=200,
            ),
        ) as client,
    ):
        login(client)
        response = client.post("/api/tts/speech", json={"text": "Hello there."})

    assert_eq(response.status_code, 200)
    assert_eq(response.content, b"provider-mp3")
    assert_eq(response.headers["content-type"], "audio/mpeg")
    assert_eq(response.headers["cache-control"], "no-store")


@test()
def speech_endpoint_requires_authentication() -> None:
    """Anonymous callers cannot spend speech-provider credits."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"provider-mp3", content_type="audio/mpeg", status_code=200
            ),
        ) as client,
    ):
        response = client.post("/api/tts/speech", json={"text": "Hello."})

    assert_eq(response.status_code, 401)


@test()
def speech_endpoint_rejects_blank_text() -> None:
    """Whitespace-only speech never reaches the provider."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"provider-mp3", content_type="audio/mpeg", status_code=200
            ),
        ) as client,
    ):
        login(client)
        response = client.post("/api/tts/speech", json={"text": "   "})

    assert_eq(response.status_code, 422)


@test()
def speech_endpoint_maps_upstream_failure() -> None:
    """A provider failure becomes a non-destructive gateway failure."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"", content_type="application/json", status_code=500
            ),
        ) as client,
    ):
        login(client)
        response = client.post("/api/tts/speech", json={"text": "Hello."})

    assert_eq(response.status_code, 502)


@test()
def speech_endpoint_maps_rate_limit_without_hint() -> None:
    """A provider 429 stays retryable when no pacing duration is supplied."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"", content_type="application/json", status_code=429
            ),
        ) as client,
    ):
        login(client)
        response = client.post("/api/tts/speech", json={"text": "Hello."})

    assert_eq(response.status_code, 503)


@test()
def speech_endpoint_preserves_rate_limit_hint() -> None:
    """A provider 429 becomes a retryable response with its pacing hint."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            response=SpeechResponse(
                audio=b"",
                content_type="application/json",
                retry_after=timedelta(seconds=12),
                status_code=429,
            ),
        ) as client,
    ):
        login(client)
        response = client.post("/api/tts/speech", json={"text": "Hello."})

    assert_eq(response.status_code, 503)
    assert_eq(response.headers["retry-after"], "12")
