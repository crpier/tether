"""Authenticated HTTP recovery routes for model-provider authorization."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.provider_auth import (
    ProviderAuthorizationActiveError,
    ProviderAuthService,
    ProviderAuthState,
    ProviderAuthStatus,
)


class ProviderAuthRead(BaseModel):
    """Browser-safe server credential or active device-code state."""

    error: str | None
    expires_in_seconds: int | None
    state: ProviderAuthState
    user_code: str | None
    verification_uri: str | None


def _response(status: ProviderAuthStatus, *, status_code: int = 200) -> JSONResponse:
    """Serialize domain state through the declared API contract."""
    body = ProviderAuthRead.model_validate(status, from_attributes=True)
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


router = APIRouter()


@router.get("/api/provider-auth/openai-codex", response_model=ProviderAuthRead)
async def provider_auth_status(request: Request) -> Response:
    """Check and refresh the server-owned OpenAI Codex credential."""
    service = cast("ProviderAuthService", request.app.state.provider_auth_service)
    return _response(await service.status())


@router.post(
    "/api/provider-auth/openai-codex", response_model=ProviderAuthRead, status_code=202
)
async def start_provider_auth(request: Request) -> Response:
    """Start OpenAI Codex device-code authorization on the server."""
    service = cast("ProviderAuthService", request.app.state.provider_auth_service)
    try:
        status = await service.start()
    except ProviderAuthorizationActiveError:
        return JSONResponse(
            {"detail": "provider authorization is already active"}, status_code=409
        )
    return _response(status, status_code=202)


@router.delete("/api/provider-auth/openai-codex", response_model=ProviderAuthRead)
async def cancel_provider_auth(request: Request) -> Response:
    """Cancel an active OpenAI Codex authorization attempt."""
    service = cast("ProviderAuthService", request.app.state.provider_auth_service)
    return _response(await service.cancel())
