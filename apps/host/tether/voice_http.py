"""HTTP parsing and failure presentation shared by audio-upload routes."""

from __future__ import annotations

from snekok.result import Err, Ok, Result
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse

from tether.stt_errors import (
    AudioUploadFailure,
    AudioUploadMalformedFailure,
    AudioUploadMissingFailure,
    AudioUploadTooLargeFailure,
    SttFailure,
    SttRateLimitedFailure,
)
from tether.stt_model import AudioUpload

MAX_AUDIO_MEGABYTES = 25
"""Maximum accepted audio upload size, matching the provider ceiling."""
MAX_AUDIO_BYTES = MAX_AUDIO_MEGABYTES * 1024 * 1024


async def read_audio_upload(
    request: Request,
) -> Result[AudioUpload, AudioUploadFailure]:
    """Parse an untrusted multipart body into a strict ephemeral audio value."""
    try:
        form = await request.form(max_part_size=MAX_AUDIO_BYTES)
    except MultiPartException:
        return Err(AudioUploadMalformedFailure())
    file_part = form.get("file")
    if not isinstance(file_part, UploadFile):
        return Err(AudioUploadMissingFailure())
    if file_part.size is not None and file_part.size > MAX_AUDIO_BYTES:
        await file_part.close()
        return Err(AudioUploadTooLargeFailure(maximum_megabytes=MAX_AUDIO_MEGABYTES))
    upload = AudioUpload(
        content=await file_part.read(),
        content_type=file_part.content_type or "application/octet-stream",
        filename=file_part.filename or "audio",
    )
    await file_part.close()
    return Ok(upload)


def audio_upload_error_response(error: AudioUploadFailure) -> JSONResponse:
    """Present one validated upload failure through the stable HTTP contract."""
    if isinstance(error, AudioUploadMalformedFailure):
        return JSONResponse({"detail": "malformed multipart upload"}, status_code=400)
    if isinstance(error, AudioUploadMissingFailure):
        return JSONResponse(
            {"detail": "a multipart 'file' audio part is required"}, status_code=422
        )
    return JSONResponse(
        {"detail": f"audio exceeds the {error.maximum_megabytes} MB limit"},
        status_code=413,
    )


def transcription_error_response(error: SttFailure) -> JSONResponse:
    """Present typed provider failures through the stable voice HTTP contract."""
    if isinstance(error, SttRateLimitedFailure) and error.retry_after is not None:
        return JSONResponse(
            {"detail": "transcription is temporarily unavailable"},
            status_code=503,
            headers={"Retry-After": str(int(error.retry_after.total_seconds()))},
        )
    return JSONResponse({"detail": "transcription failed"}, status_code=502)


__all__ = [
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_MEGABYTES",
    "audio_upload_error_response",
    "read_audio_upload",
    "transcription_error_response",
]
