"""Authenticated HTTP routes for YouTube authorization recovery."""

from __future__ import annotations

from fastapi import APIRouter
from snekok import Err
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from tether.app_runtime import app_runtime
from tether.youtube_auth_service import YouTubeAuthStatus

router = APIRouter()


@router.get("/api/youtube-auth", response_model=YouTubeAuthStatus)
async def youtube_auth_status(request: Request) -> YouTubeAuthStatus:
    """Report whether the server has usable YouTube authorization."""
    return await app_runtime(request.app).youtube_auth_service.status()


@router.post(
    "/api/youtube-auth",
    response_model=YouTubeAuthStatus,
    status_code=202,
)
async def start_youtube_auth(request: Request) -> YouTubeAuthStatus:
    """Create a Google consent request for the authenticated browser."""
    return await app_runtime(request.app).youtube_auth_service.start(
        redirect_uri=f"{request.url_for('youtube_auth_status')}/callback"
    )


@router.get("/api/youtube-auth/callback", include_in_schema=False)
async def complete_youtube_auth(request: Request, state: str) -> Response:
    """Complete Google consent after validating the pending OAuth state."""
    outcome = await app_runtime(request.app).youtube_auth_service.complete(
        authorization_response=str(request.url),
        state=state,
    )
    if isinstance(outcome, Err):
        return JSONResponse({"detail": outcome.error.message}, status_code=400)
    return RedirectResponse(
        "/settings?youtube_auth=connected",
        status_code=303,
    )
