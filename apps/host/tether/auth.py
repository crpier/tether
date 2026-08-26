"""Middleware ownership for cookie and bearer authenticated API requests."""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from tether.auth_sessions import (
    SESSION_COOKIE,
    authenticate_bearer_token,
    set_session_cookie,
    verify_session_cookie,
)


class AppSessionMiddleware(BaseHTTPMiddleware):
    """Require valid app authentication for browser-facing REST routes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        secure: bool,
        session_secret: str,
        api_token: str = "",
    ) -> None:
        super().__init__(app)
        self.api_token: str = api_token
        self.secure: bool = secure
        self.session_secret: str = session_secret

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Gate `/api/*` except auth routes, accepting bearer or cookie auth."""
        if not request.url.path.startswith("/api/") or request.url.path.startswith(
            "/api/auth/"
        ):
            return await call_next(request)
        bearer_principal = authenticate_bearer_token(
            request.headers.get("Authorization", ""), self.api_token
        )
        if bearer_principal is not None:
            request.state.principal = bearer_principal
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
