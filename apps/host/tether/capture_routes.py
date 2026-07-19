"""HTTP route for voice capture: an audio note transcribed into a Memory.

`POST /api/capture/voice` accepts a multipart audio upload (m4a/ogg/wav),
transcribes it through the shared STT capability, and captures the transcript
on the existing memory path. A voice note is the human's own assertion, but
transcription can err, so it lands loose (plain `capture`, not tethered) with a
`voice` provenance and a `source: voice` facet — Review calibrates from there.
The audio itself is never persisted; it exists only for the length of the
request. When the STT capability is unconfigured the endpoint is a 503.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether import memory_capabilities
from tether.memories import EmptyMemoryContentError, MemoryProvenance
from tether.memory_capabilities import MemoryRead
from tether.openapi import EndpointRoute, endpoint
from tether.stt import AudioUpload, SttClient, SttError

_MAX_AUDIO_MEGABYTES = 25
"""The upstream OpenAI transcription upload ceiling; larger uploads are rejected."""
_MAX_AUDIO_BYTES = _MAX_AUDIO_MEGABYTES * 1024 * 1024
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


async def _read_audio_upload(request: Request) -> AudioUpload | JSONResponse:
    """Parse the multipart `file` part into an `AudioUpload`, or an error response.

    Enforces the upload contract before any transcription: a well-formed
    multipart body carrying a `file` audio part no larger than the 25 MB
    ceiling. A malformed body, a missing part, or an oversize upload each
    resolve to the matching HTTP error instead of an upload.
    """
    try:
        form = await request.form(max_part_size=_MAX_AUDIO_BYTES)
    except MultiPartException:
        return JSONResponse({"detail": "malformed multipart upload"}, status_code=400)
    file_part = form.get("file")
    if not isinstance(file_part, UploadFile):
        return JSONResponse(
            {"detail": "a multipart 'file' audio part is required"}, status_code=422
        )
    if file_part.size is not None and file_part.size > _MAX_AUDIO_BYTES:
        await file_part.close()
        return JSONResponse(
            {"detail": f"audio exceeds the {_MAX_AUDIO_MEGABYTES} MB limit"},
            status_code=413,
        )
    upload = AudioUpload(
        content=await file_part.read(),
        content_type=file_part.content_type or "application/octet-stream",
        filename=file_part.filename or "audio",
    )
    await file_part.close()
    return upload


def _transcription_error_response(error: SttError) -> JSONResponse:
    """Translate an upstream transcription failure into an HTTP response.

    A rate limit becomes a 503 carrying the parsed `Retry-After` so the caller
    can pace a retry; any other upstream failure is a 502. Either way the audio
    was not captured — the client keeps the recording to try again.
    """
    if error.retry_after is not None:
        return JSONResponse(
            {"detail": "transcription is temporarily unavailable"},
            status_code=503,
            headers={"Retry-After": str(int(error.retry_after.total_seconds()))},
        )
    return JSONResponse({"detail": "transcription failed"}, status_code=502)


@endpoint(response=VoiceCaptureResponse, status=201)
async def capture_voice(request: Request) -> Response:
    """Transcribe an uploaded audio note and capture it as a loose Memory."""
    stt_client = cast("SttClient | None", request.app.state.stt_client)
    if stt_client is None:
        return JSONResponse(
            {"detail": "voice capture is not configured"}, status_code=503
        )
    audio = await _read_audio_upload(request)
    if isinstance(audio, JSONResponse):
        return audio
    try:
        transcript = await stt_client.transcribe(audio)
    except SttError as error:
        return _transcription_error_response(error)
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
