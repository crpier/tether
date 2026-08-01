"""Behaviour tests for the voice-capture REST endpoint.

These drive `POST /api/capture/voice` through the app's test client, wiring a
real `SttClient` over a scripted `FakeSttTransport` so no live transcription
call runs. They assert the endpoint contract: a successful transcription is
captured as a loose, human-asserted voice Memory (transcript echoed back); an
oversize upload is rejected; a silent recording (empty transcript) is a 422;
and a rate-limited upstream surfaces as a 503 carrying `Retry-After`. STT is an
always-on host dependency (ADR 0018), so there is no unconfigured/503 path to
test here.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.stt import AudioUpload, SttClient, TranscriptionResponse
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


class ScriptedSttTransport:
    """A scripted `SttTransport` returning one queued transcription response."""

    def __init__(self, response: TranscriptionResponse) -> None:
        self.response: TranscriptionResponse = response

    async def transcribe(
        self, *, audio: AudioUpload, model: str
    ) -> TranscriptionResponse:
        """Return the scripted response regardless of the upload."""
        return self.response


def _stt_client(response: TranscriptionResponse) -> SttClient:
    """A real client over a scripted transport, as the endpoint would use."""
    return SttClient(transport=ScriptedSttTransport(response), model="whisper-1")


def make_client(root: Path, *, stt_client: SttClient) -> TestClient:
    """Create a voice-capable app with an injected (scripted) STT client.

    STT is an always-on host dependency (ADR 0018), so every test wires a
    scripted `SttClient` rather than exercising a disabled/unconfigured path.
    """
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                stt_client=stt_client,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def post_voice(client: TestClient, audio: bytes) -> Any:
    """POST an audio note as multipart `file` to the voice endpoint."""
    return client.post(
        "/api/capture/voice",
        files={"file": ("note.m4a", audio, "audio/mp4")},
    )


def loose_memory_count(client: TestClient) -> int:
    """The number of loose memories currently captured."""
    response = client.get("/api/memories", params={"state": "loose"})
    assert_eq(response.status_code, 200)
    return len(response.json())


def default_conversation_id(client: TestClient) -> str:
    """Return the lazily created default conversation id."""
    response = client.get("/api/conversations")
    assert_eq(response.status_code, 200)
    return str(response.json()[0]["id"])


@test()
def voice_capture_transcribes_and_returns_the_transcript() -> None:
    """A recognized note is echoed back as the response transcript."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(
                TranscriptionResponse(status_code=200, text="buy oat milk")
            ),
        ) as client,
    ):
        login(client)
        response = post_voice(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["transcript"], "buy oat milk")


@test()
def voice_capture_adds_a_user_chat_turn_without_creating_memory() -> None:
    """A voice capture enters chat and does not create a direct Memory row."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(
                TranscriptionResponse(status_code=200, text="call the dentist")
            ),
        ) as client,
    ):
        login(client)
        conversation_id = default_conversation_id(client)
        before = loose_memory_count(client)
        response = post_voice(client, b"fake-audio-bytes")
        after = loose_memory_count(client)
        messages = client.get(f"/api/conversations/{conversation_id}/messages")

    assert_eq(response.status_code, 201)
    assert_eq(before, 0)
    assert_eq(after, 0)
    assert_eq(messages.status_code, 200)
    assert_eq(messages.json()[0]["role"], "user")
    assert_eq(messages.json()[0]["content"], "call the dentist")


@test()
def voice_capture_rejects_audio_over_the_size_limit() -> None:
    """An upload larger than 25 MB is rejected before transcription."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="x")),
        ) as client,
    ):
        login(client)
        response = post_voice(client, b"\0" * (_MAX_AUDIO_BYTES + 1))

    assert_eq(response.status_code, 413)


@test()
def voice_capture_treats_a_silent_recording_as_unprocessable() -> None:
    """An empty transcript captures nothing and reports 422."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="   ")),
        ) as client,
    ):
        login(client)
        response = post_voice(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 422)


@test()
def voice_capture_surfaces_a_rate_limit_with_retry_after() -> None:
    """A rate-limited upstream becomes a 503 carrying the `Retry-After` hint."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(
                TranscriptionResponse(
                    status_code=429, text="", retry_after=timedelta(seconds=30)
                )
            ),
        ) as client,
    ):
        login(client)
        response = post_voice(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 503)
    assert_eq(response.headers.get("retry-after"), "30")


@test()
def voice_capture_requires_authentication() -> None:
    """An anonymous voice upload is gated like any public REST route."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="x")),
        ) as client,
    ):
        response = post_voice(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 401)
