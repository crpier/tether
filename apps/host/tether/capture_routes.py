"""HTTP route for voice capture: an audio note transcribed into chat.

`POST /api/capture/voice` accepts a multipart audio upload (m4a/ogg/wav),
transcribes it through the shared STT capability, and appends the transcript as
an ordinary user chat turn. The audio itself is never persisted; it exists only
for the length of the request. STT is an always-on host dependency, so this
endpoint has no unconfigured/503 path.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from snekok import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft
from tether.conversation_routes import MessageRead
from tether.voice_http import (
    audio_upload_error_response,
    read_audio_upload,
    transcription_error_response,
)


class VoiceCaptureResponse(BaseModel):
    """A transcribed voice note and the user chat message it appended."""

    message: MessageRead
    transcript: str


router = APIRouter()


@router.post("/api/capture/voice", response_model=VoiceCaptureResponse, status_code=201)
async def capture_voice(request: Request) -> Response:
    """Transcribe an uploaded audio note and append it as a user chat turn."""
    stt_client = app_runtime(request.app).stt_client
    upload_outcome = await read_audio_upload(request)
    if isinstance(upload_outcome, Err):
        return audio_upload_error_response(upload_outcome.error)
    transcription_outcome = await stt_client.transcribe(upload_outcome.value)
    if isinstance(transcription_outcome, Err):
        return transcription_error_response(transcription_outcome.error)
    normalised_transcript = transcription_outcome.value.strip()
    if not normalised_transcript:
        return JSONResponse(
            {"detail": "no speech detected in the audio"}, status_code=422
        )
    conversation = (
        await app_runtime(request.app).conversation_service.list_conversations()
    )[0]
    message = await app_runtime(request.app).conversation_service.append_message(
        MessageDraft(
            content=normalised_transcript,
            conversation_id=conversation.id,
            role="user",
        )
    )
    return JSONResponse(
        {
            "transcript": normalised_transcript,
            "message": MessageRead.from_message(message).model_dump(mode="json"),
        },
        status_code=201,
    )
