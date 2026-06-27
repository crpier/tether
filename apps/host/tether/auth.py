"""Single-password app auth and stateless signed session cookies."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from itsdangerous import BadData, URLSafeSerializer
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from tether.openapi import EndpointRoute, endpoint

SESSION_COOKIE = "tether_session"
"""Browser cookie name carrying the signed app session token."""

_SESSION_SUBJECT = "app"
_SESSION_TTL = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated app identity carried by sessions.

    ```python
    principal = Principal(sub="app")
    assert principal.sub == "app"
    ```
    """

    sub: str


class LoginRequest(BaseModel):
    """Body for app login with the shared password."""

    password: str


class SessionResponse(BaseModel):
    """Whether the request carries a currently valid app session."""

    authenticated: bool


def _session_serializer(session_secret: str) -> URLSafeSerializer:
    """Create the signer used for stateless app session cookies."""
    return URLSafeSerializer(session_secret, salt="tether-session")


def authenticate_password(password: str, configured_password: str) -> Principal | None:
    """Validate the single app password in constant time."""
    if secrets.compare_digest(password.encode(), configured_password.encode()):
        return Principal(sub=_SESSION_SUBJECT)
    return None


def mint_session_cookie(
    principal: Principal,
    session_secret: str,
    *,
    issued_at: datetime | None = None,
) -> str:
    """Sign a stateless session token with 30-day absolute claims."""
    now = issued_at or datetime.now(UTC)
    issued_timestamp = int(now.timestamp())
    return str(
        _session_serializer(session_secret).dumps(
            {
                "sub": principal.sub,
                "iat": issued_timestamp,
                "exp": int((now + _SESSION_TTL).timestamp()),
            }
        )
    )


def verify_session_cookie(
    token: str,
    session_secret: str,
    *,
    now: datetime | None = None,
) -> Principal | None:
    """Verify a signed session cookie and return its principal if current."""
    try:
        loaded_claims: object = _session_serializer(session_secret).loads(token)
    except BadData:
        return None
    if not isinstance(loaded_claims, dict):
        return None
    claims = cast("dict[str, object]", loaded_claims)
    sub = claims.get("sub")
    expires_at = claims.get("exp")
    if not isinstance(sub, str) or not isinstance(expires_at, int):
        return None
    if expires_at <= int((now or datetime.now(UTC)).timestamp()):
        return None
    return Principal(sub=sub)


def set_session_cookie(
    response: Response,
    principal: Principal,
    session_secret: str,
    *,
    secure: bool,
) -> None:
    """Attach a refreshed app session cookie to a response."""
    response.set_cookie(
        SESSION_COOKIE,
        mint_session_cookie(principal, session_secret),
        httponly=True,
        max_age=int(_SESSION_TTL.total_seconds()),
        path="/",
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Expire the app session cookie in the browser."""
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        path="/",
        samesite="lax",
        secure=secure,
    )


class AppSessionMiddleware(BaseHTTPMiddleware):
    """Require a valid app session for browser-facing REST routes."""

    def __init__(self, app: ASGIApp, *, secure: bool, session_secret: str) -> None:
        super().__init__(app)
        self.secure: bool = secure
        self.session_secret: str = session_secret

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Gate `/api/*` except auth routes, refreshing valid sessions."""
        if not request.url.path.startswith("/api/") or request.url.path.startswith(
            "/api/auth/"
        ):
            return await call_next(request)
        principal = verify_session_cookie(
            request.cookies.get(SESSION_COOKIE, ""), self.session_secret
        )
        if principal is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        request.state.principal = principal
        response = await call_next(request)
        set_session_cookie(
            response,
            principal,
            self.session_secret,
            secure=self.secure,
        )
        return response


@endpoint(request_body=LoginRequest, status=204)
async def login(request: Request, body: LoginRequest) -> Response:
    """Authenticate with the app password and set a session cookie."""
    principal = authenticate_password(
        body.password, cast("str", request.app.state.app_password)
    )
    if principal is None:
        return JSONResponse({"detail": "invalid password"}, status_code=401)
    response = Response(status_code=204)
    set_session_cookie(
        response,
        principal,
        cast("str", request.app.state.session_secret),
        secure=cast("bool", request.app.state.secure_cookies),
    )
    return response


@endpoint(response=SessionResponse)
async def session(request: Request) -> Response:
    """Report whether the request carries a valid app session."""
    principal = verify_session_cookie(
        request.cookies.get(SESSION_COOKIE, ""),
        cast("str", request.app.state.session_secret),
    )
    response = JSONResponse({"authenticated": principal is not None})
    if principal is not None:
        set_session_cookie(
            response,
            principal,
            cast("str", request.app.state.session_secret),
            secure=cast("bool", request.app.state.secure_cookies),
        )
    return response


@endpoint(status=204)
async def logout(request: Request) -> Response:
    """Clear the app session cookie."""
    response = Response(status_code=204)
    clear_session_cookie(
        response, secure=cast("bool", request.app.state.secure_cookies)
    )
    return response


auth_routes: list[Route] = [
    EndpointRoute("/api/auth/login", login, methods=["POST"]),
    EndpointRoute("/api/auth/logout", logout, methods=["POST"]),
    EndpointRoute("/api/auth/session", session, methods=["GET"]),
]
