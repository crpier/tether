"""HTTP login, session-status, and logout presentation."""

from typing import Protocol, cast

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.auth_sessions import (
    SESSION_COOKIE,
    authenticate_password,
    clear_session_cookie,
    set_session_cookie,
    verify_session_cookie,
)


class _AuthRuntime(Protocol):
    """Authentication values available while the host serves requests."""

    app_password: str
    secure_cookies: bool
    session_secret: str


def _runtime(request: Request) -> _AuthRuntime:
    """Read authentication dependencies from the canonical host runtime."""
    return cast("_AuthRuntime", request.app.state.runtime)


class LoginRequest(BaseModel):
    """Body for app login with the shared password."""

    password: str


class SessionResponse(BaseModel):
    """Whether the request carries a currently valid app session."""

    authenticated: bool


router = APIRouter()


@router.post("/api/auth/login", status_code=204)
async def login(request: Request, body: LoginRequest) -> Response:
    """Authenticate with the app password and set a session cookie."""
    principal = authenticate_password(body.password, _runtime(request).app_password)
    if principal is None:
        return JSONResponse({"detail": "invalid password"}, status_code=401)
    response = Response(status_code=204)
    set_session_cookie(
        response,
        principal,
        _runtime(request).session_secret,
        secure=_runtime(request).secure_cookies,
    )
    return response


@router.get("/api/auth/session", response_model=SessionResponse)
async def session(request: Request) -> Response:
    """Report whether the request carries a valid app session."""
    principal = verify_session_cookie(
        request.cookies.get(SESSION_COOKIE, ""),
        _runtime(request).session_secret,
    )
    response = JSONResponse({"authenticated": principal is not None})
    if principal is not None:
        set_session_cookie(
            response,
            principal,
            _runtime(request).session_secret,
            secure=_runtime(request).secure_cookies,
        )
    return response


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    """Clear the app session cookie."""
    response = Response(status_code=204)
    clear_session_cookie(response, secure=_runtime(request).secure_cookies)
    return response
