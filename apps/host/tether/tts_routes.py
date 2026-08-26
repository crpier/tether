"""Authenticated HTTP presentation for provider-generated speech."""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from snekok import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.app_runtime import app_runtime
from tether.tts_errors import TtsFailure, TtsRateLimitedFailure


class SpeechRequest(BaseModel):
    """Text to render as ephemeral speech audio."""

    text: str = Field(min_length=1, max_length=4096)


router = APIRouter()


def _speech_error_response(error: TtsFailure) -> JSONResponse:
    """Present typed provider failures without exposing provider response bodies."""
    if isinstance(error, TtsRateLimitedFailure):
        headers = (
            {"Retry-After": str(int(error.retry_after.total_seconds()))}
            if error.retry_after is not None
            else None
        )
        return JSONResponse(
            {"detail": "speech generation is temporarily unavailable"},
            status_code=503,
            headers=headers,
        )
    return JSONResponse({"detail": "speech generation failed"}, status_code=502)


@router.post("/api/tts/speech", response_class=Response)
async def synthesize_speech(body: SpeechRequest, request: Request) -> Response:
    """Generate audio for one normalized spoken reply fragment."""
    if not body.text.strip():
        return JSONResponse({"detail": "speech text cannot be blank"}, status_code=422)
    outcome = await app_runtime(request.app).tts_client.synthesize(body.text)
    if isinstance(outcome, Err):
        return _speech_error_response(outcome.error)
    return Response(
        content=outcome.value.audio,
        headers={"Cache-Control": "no-store"},
        media_type=outcome.value.content_type,
    )
