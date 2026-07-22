"""Shared HTTP helpers for audio-upload routes (voice capture, transcribe-only).

Both `capture_routes.capture_voice` and `stt_routes.transcribe_audio` accept a
multipart audio upload and must reject it consistently (oversize, malformed,
missing) and translate an upstream `SttError` into the same HTTP shape. Kept
here so the two routes can't drift on the 413/422/502/503+Retry-After contract.
"""

from __future__ import annotations

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse

from tether.stt import AudioUpload, SttError

MAX_AUDIO_MEGABYTES = 25
"""The upstream OpenAI transcription upload ceiling; larger uploads are rejected."""
MAX_AUDIO_BYTES = MAX_AUDIO_MEGABYTES * 1024 * 1024


async def read_audio_upload(request: Request) -> AudioUpload | JSONResponse:
    """Parse the multipart `file` part into an `AudioUpload`, or an error response.

    Enforces the upload contract before any transcription: a well-formed
    multipart body carrying a `file` audio part no larger than the 25 MB
    ceiling. A malformed body, a missing part, or an oversize upload each
    resolve to the matching HTTP error instead of an upload. Any audio
    container the caller sends (m4a/ogg/wav, or a browser MediaRecorder
    webm/opus blob) is accepted — the STT provider sniffs the container itself.
    """
    try:
        form = await request.form(max_part_size=MAX_AUDIO_BYTES)
    except MultiPartException:
        return JSONResponse({"detail": "malformed multipart upload"}, status_code=400)
    file_part = form.get("file")
    if not isinstance(file_part, UploadFile):
        return JSONResponse(
            {"detail": "a multipart 'file' audio part is required"}, status_code=422
        )
    if file_part.size is not None and file_part.size > MAX_AUDIO_BYTES:
        await file_part.close()
        return JSONResponse(
            {"detail": f"audio exceeds the {MAX_AUDIO_MEGABYTES} MB limit"},
            status_code=413,
        )
    upload = AudioUpload(
        content=await file_part.read(),
        content_type=file_part.content_type or "application/octet-stream",
        filename=file_part.filename or "audio",
    )
    await file_part.close()
    return upload


def transcription_error_response(error: SttError) -> JSONResponse:
    """Translate an upstream transcription failure into an HTTP response.

    A rate limit (or any upstream failure carrying a `Retry-After` hint)
    becomes a 503 carrying the parsed delay so the caller can pace a retry; any
    other upstream failure is a 502. Either way the audio was not captured —
    the client keeps the recording to try again.
    """
    if error.retry_after is not None:
        return JSONResponse(
            {"detail": "transcription is temporarily unavailable"},
            status_code=503,
            headers={"Retry-After": str(int(error.retry_after.total_seconds()))},
        )
    return JSONResponse({"detail": "transcription failed"}, status_code=502)


__all__ = [
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_MEGABYTES",
    "read_audio_upload",
    "transcription_error_response",
]
