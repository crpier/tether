"""HTTP route for voice capture: an audio note transcribed into a Memory.

`POST /api/capture/voice` accepts a multipart audio upload (m4a/ogg/wav),
transcribes it through the shared STT capability, and captures the transcript
on the existing memory path. A voice note is the human's own assertion, but
transcription can err, so it lands loose (plain `capture`, not tethered) with a
`voice` provenance and a `source: voice` facet — Review calibrates from there.
The audio itself is never persisted; it exists only for the length of the
request. STT is an always-on host dependency (ADR 0018), so this endpoint has
no unconfigured/503 path.

This is the "dumb client" capture path (spec #225) — memory-direct, no chat
involvement. The web chat composer instead uses the transcribe-only route in
`stt_routes.py`, which returns just a transcript with no Memory side effect
(issue #19); rewiring this endpoint onto chat is explicitly out of scope here
(issue #239).
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether import memory_capabilities
from tether.memories import EmptyMemoryContentError, MemoryProvenance
from tether.memory_capabilities import MemoryRead
from tether.openapi import EndpointRoute, endpoint
from tether.stt import SttClient, SttError
from tether.voice_http import read_audio_upload, transcription_error_response

_VOICE_FACETS = {"source": "voice"}
"""The Commons facet set stamped on every voice-captured Memory."""


class VoiceCaptureResponse(BaseModel):
    """A transcribed voice note and the loose Memory it captured.

    >>> VoiceCaptureResponse(
    ...     memory=MemoryRead(
    ...         content="buy oat milk",
    ...         created_at=datetime(2026, 1, 1),
    ...         facets={"source": "voice"},
    ...         id="018f0000-0000-7000-8000-000000000000",
    ...         state="loose",
    ...         tethered_at=None,
    ...         updated_at=datetime(2026, 1, 1),
    ...         version=1,
    ...     ),
    ...     transcript="buy oat milk",
    ... ).transcript
    'buy oat milk'
    """

    memory: MemoryRead
    transcript: str


@endpoint(response=VoiceCaptureResponse, status=201)
async def capture_voice(request: Request) -> Response:
    """Transcribe an uploaded audio note and capture it as a loose Memory."""
    stt_client = cast("SttClient", request.app.state.stt_client)
    audio = await read_audio_upload(request)
    if isinstance(audio, JSONResponse):
        return audio
    try:
        transcript = await stt_client.transcribe(audio)
    except SttError as error:
        return transcription_error_response(error)
    try:
        outcome = await memory_capabilities.capture(
            request,
            transcript,
            facets=dict(_VOICE_FACETS),
            provenance=MemoryProvenance(kind="voice"),
        )
    except EmptyMemoryContentError:
        return JSONResponse(
            {"detail": "no speech detected in the audio"}, status_code=422
        )
    return JSONResponse(
        {"transcript": transcript, "memory": outcome.result}, status_code=201
    )


capture_routes: list[Route] = [
    EndpointRoute("/api/capture/voice", capture_voice, methods=["POST"]),
]
