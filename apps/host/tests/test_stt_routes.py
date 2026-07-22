"""Behaviour tests for the transcribe-only REST endpoint (issue #19).

`POST /api/stt/transcriptions` is a thin wrapper over the shared `SttClient`:
it returns the transcript text only, with no Memory created and no chat turn
injected server-side. These tests wire a real `SttClient` over a scripted
transport (mirroring `test_capture_routes.py`) so no live transcription call
runs, and assert the same error-mapping contract as `/api/capture/voice`
(413/422/502/503+Retry-After) plus the "no side effect" guarantee that
distinguishes this route from the memory-direct voice-capture endpoint.
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
    """Create an app with an injected (scripted) STT client."""
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


def post_transcription(client: TestClient, audio: bytes) -> Any:
    """POST an audio clip as multipart `file` to the transcribe-only endpoint."""
    return client.post(
        "/api/stt/transcriptions",
        files={"file": ("clip.webm", audio, "audio/webm")},
    )


def loose_memory_count(client: TestClient) -> int:
    """The number of loose memories currently captured, for no-side-effect checks."""
    response = client.get("/api/memories", params={"state": "loose"})
    assert_eq(response.status_code, 200)
    return len(response.json())


@test()
def transcribe_returns_the_transcript_only() -> None:
    """A recognized clip returns just the transcript text."""
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
        response = post_transcription(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 201)
    assert_eq(response.json(), {"transcript": "buy oat milk"})


@test()
def transcribe_creates_no_memory_and_injects_no_chat_turn() -> None:
    """A successful transcription has no Memory side effect."""
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
        before = loose_memory_count(client)
        _ = post_transcription(client, b"fake-audio-bytes")
        after = loose_memory_count(client)

    assert_eq(before, 0)
    assert_eq(after, 0)


@test()
def transcribe_accepts_a_browser_mediarecorder_webm_upload() -> None:
    """A webm/opus container (browser `MediaRecorder` output) transcribes fine."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="ok")),
        ) as client,
    ):
        login(client)
        response = client.post(
            "/api/stt/transcriptions",
            files={"file": ("clip.webm", b"fake-webm-bytes", "audio/webm;codecs=opus")},
        )

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["transcript"], "ok")


@test()
def transcribe_rejects_audio_over_the_size_limit() -> None:
    """An upload larger than 25 MB is rejected before transcription."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="x")),
        ) as client,
    ):
        login(client)
        response = post_transcription(client, b"\0" * (_MAX_AUDIO_BYTES + 1))

    assert_eq(response.status_code, 413)


@test()
def transcribe_treats_a_silent_recording_as_unprocessable() -> None:
    """An empty transcript is reported as a 422, not a false-successful send."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="   ")),
        ) as client,
    ):
        login(client)
        response = post_transcription(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 422)


@test()
def transcribe_surfaces_an_upstream_failure_as_502() -> None:
    """A non-rate-limit upstream failure surfaces as a 502."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=500, text="")),
        ) as client,
    ):
        login(client)
        response = post_transcription(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 502)


@test()
def transcribe_surfaces_a_rate_limit_with_retry_after() -> None:
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
        response = post_transcription(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 503)
    assert_eq(response.headers.get("retry-after"), "30")


@test()
def transcribe_requires_authentication() -> None:
    """An anonymous upload is gated like any public REST route."""
    with (
        TemporaryDirectory() as directory,
        make_client(
            Path(directory),
            stt_client=_stt_client(TranscriptionResponse(status_code=200, text="x")),
        ) as client,
    ):
        response = post_transcription(client, b"fake-audio-bytes")

    assert_eq(response.status_code, 401)
