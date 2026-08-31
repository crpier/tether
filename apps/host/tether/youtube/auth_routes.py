"""Authenticated HTTP routes for YouTube authorization recovery."""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter
from snekok.result import Err
from starlette.datastructures import URL
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from tether.youtube.auth_service import YouTubeAuthService, YouTubeAuthStatus


class _YouTubeAuthRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    youtube_auth_service: YouTubeAuthService
    public_origin: str


def _runtime(request: Request) -> _YouTubeAuthRuntime:
    """Read the installed application runtime off the request."""
    return cast("_YouTubeAuthRuntime", request.app.state.runtime)


router = APIRouter()


def _external_url(request: Request, url: URL) -> str:
    """Use the configured browser origin across an HTTPS-terminating proxy."""
    public_origin = _runtime(request).public_origin.rstrip("/")
    if not public_origin:
        return str(url)
    query = f"?{url.query}" if url.query else ""
    return f"{public_origin}{url.path}{query}"


@router.get("/api/youtube-auth", response_model=YouTubeAuthStatus)
async def youtube_auth_status(request: Request) -> YouTubeAuthStatus:
    """Report whether the server has usable YouTube authorization."""
    return await _runtime(request).youtube_auth_service.status()


@router.post(
    "/api/youtube-auth",
    response_model=YouTubeAuthStatus,
    status_code=202,
)
async def start_youtube_auth(request: Request) -> YouTubeAuthStatus:
    """Create a Google consent request for the authenticated browser."""
    status_url = _external_url(request, request.url_for("youtube_auth_status"))
    return await _runtime(request).youtube_auth_service.start(
        redirect_uri=f"{status_url}/callback"
    )


@router.get("/api/youtube-auth/callback", include_in_schema=False)
async def complete_youtube_auth(request: Request, state: str) -> Response:
    """Complete Google consent after validating the pending OAuth state."""
    outcome = await _runtime(request).youtube_auth_service.complete(
        authorization_response=_external_url(request, request.url),
        state=state,
    )
    if isinstance(outcome, Err):
        return JSONResponse({"detail": outcome.error.message}, status_code=400)
    return RedirectResponse(
        "/settings?youtube_auth=connected",
        status_code=303,
    )
