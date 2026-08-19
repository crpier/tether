"""Authenticated HTTP routes for Gmail authorization recovery."""

from __future__ import annotations

from fastapi import APIRouter
from snekok import Err
from starlette.datastructures import URL
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from tether.app_runtime import app_runtime
from tether.gmail_auth_service import GmailAuthStatus

router = APIRouter()


def _external_url(request: Request, url: URL) -> str:
    """Use the configured browser origin across an HTTPS-terminating proxy."""
    public_origin = app_runtime(request.app).public_origin.rstrip("/")
    if not public_origin:
        return str(url)
    query = f"?{url.query}" if url.query else ""
    return f"{public_origin}{url.path}{query}"


@router.get("/api/gmail-auth", response_model=GmailAuthStatus)
async def gmail_auth_status(request: Request) -> GmailAuthStatus:
    """Report whether the server has usable Gmail authorization."""
    auth_service = app_runtime(request.app).gmail_auth_service
    if auth_service is None:
        return GmailAuthStatus(
            error="Gmail authorization is not configured.",
            state="error",
        )
    return await auth_service.status()


@router.post(
    "/api/gmail-auth",
    response_model=GmailAuthStatus,
    status_code=202,
)
async def start_gmail_auth(request: Request) -> GmailAuthStatus:
    """Create a Google consent request for the authenticated browser."""
    status_url = _external_url(request, request.url_for("gmail_auth_status"))
    auth_service = app_runtime(request.app).gmail_auth_service
    if auth_service is None:
        return GmailAuthStatus(
            error="Gmail authorization is not configured.",
            state="error",
        )
    return await auth_service.start(redirect_uri=f"{status_url}/callback")


@router.get("/api/gmail-auth/callback", include_in_schema=False)
async def complete_gmail_auth(request: Request, state: str) -> Response:
    """Complete Google consent after validating the pending OAuth state."""
    auth_service = app_runtime(request.app).gmail_auth_service
    if auth_service is None:
        return JSONResponse(
            {"detail": "Gmail authorization is not configured."},
            status_code=400,
        )
    outcome = await auth_service.complete(
        authorization_response=_external_url(request, request.url),
        state=state,
    )
    if isinstance(outcome, Err):
        return JSONResponse({"detail": outcome.error.message}, status_code=400)
    return RedirectResponse(
        "/settings?gmail_auth=connected",
        status_code=303,
    )
