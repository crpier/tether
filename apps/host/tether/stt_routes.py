"""HTTP route for transcribe-only speech-to-text: `POST /api/stt/transcriptions`.

Thin wrapper over the shared `SttClient` (`tether/stt.py`): accepts a multipart
audio upload and returns the transcript text only — no Memory is created and no
chat turn is injected server-side. This is the route the web chat composer's
voice buttons use (issue #19): the client decides what to do with the
transcript (fill the composer for review, or send it immediately through the
normal client-side chat-send path). Contrast with `POST /api/capture/voice`
(`capture_routes.py`), which is memory-direct for dumb capture clients and is
unaffected by this route.

Accepts any audio container the caller sends — the existing m4a/ogg/wav shapes
from capture clients as well as the webm/opus blobs a browser `MediaRecorder`
produces — since the STT provider sniffs the container itself. Session-cookie
authenticated like the rest of the browser-facing API (no separate auth path).
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.openapi import EndpointRoute, endpoint
from tether.stt import SttClient, SttError
from tether.voice_http import read_audio_upload, transcription_error_response


class TranscriptionResponse(BaseModel):
    """The recognized transcript text for one uploaded audio clip.

    >>> TranscriptionResponse(transcript="buy oat milk").transcript
    'buy oat milk'
    """

    transcript: str


@endpoint(response=TranscriptionResponse, status=201)
async def transcribe_audio(request: Request) -> Response:
    """Transcribe an uploaded audio clip and return the transcript text only."""
    stt_client = cast("SttClient", request.app.state.stt_client)
    audio = await read_audio_upload(request)
    if isinstance(audio, JSONResponse):
        return audio
    try:
        transcript = await stt_client.transcribe(audio)
    except SttError as error:
        return transcription_error_response(error)
    if not transcript.strip():
        return JSONResponse(
            {"detail": "no speech detected in the audio"}, status_code=422
        )
    return JSONResponse({"transcript": transcript}, status_code=201)


stt_routes: list[Route] = [
    EndpointRoute("/api/stt/transcriptions", transcribe_audio, methods=["POST"]),
]
