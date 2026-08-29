"""Transcribe-only HTTP presentation for browser voice input.

The route returns recognized text without creating a Memory or injecting a chat
turn. The browser decides whether to review the text or send it through the
ordinary chat path.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from snekok.result import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.app_runtime import app_runtime
from tether.voice_http import (
    audio_upload_error_response,
    read_audio_upload,
    transcription_error_response,
)


class TranscriptionResponse(BaseModel):
    """The recognized transcript text for one uploaded audio clip.

    >>> TranscriptionResponse(transcript="buy oat milk").transcript
    'buy oat milk'
    """

    transcript: str


router = APIRouter()


@router.post(
    "/api/stt/transcriptions", response_model=TranscriptionResponse, status_code=201
)
async def transcribe_audio(request: Request) -> Response:
    """Transcribe an uploaded audio clip and return the transcript text only."""
    stt_client = app_runtime(request.app).stt_client
    upload_outcome = await read_audio_upload(request)
    if isinstance(upload_outcome, Err):
        return audio_upload_error_response(upload_outcome.error)
    transcription_outcome = await stt_client.transcribe(upload_outcome.unwrap())
    if isinstance(transcription_outcome, Err):
        return transcription_error_response(transcription_outcome.error)
    transcript = transcription_outcome.unwrap()
    if not transcript.strip():
        return JSONResponse(
            {"detail": "no speech detected in the audio"}, status_code=422
        )
    return JSONResponse({"transcript": transcript}, status_code=201)
