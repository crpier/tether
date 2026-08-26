"""Independent path-specific bearer authentication middleware."""

import hmac
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


def _authorized(request: Request, token: str) -> bool:
    """Compare one bearer credential without accepting an empty configuration."""
    scheme, _, offered = request.headers.get("Authorization", "").partition(" ")
    return (
        bool(token)
        and scheme.casefold() == "bearer"
        and hmac.compare_digest(offered, token)
    )


class CaptureAuthMiddleware(BaseHTTPMiddleware):
    """Protect only Android Health Connect ingestion with its bearer token."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        super().__init__(app)
        self.token: str = token

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Reject unauthorized requests under the retained capture prefix."""
        if not request.url.path.startswith("/api/telemetry/health-connect/"):
            return await call_next(request)
        if not _authorized(request, self.token):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)


class OpenWebUIToolAuthMiddleware(BaseHTTPMiddleware):
    """Protect Open WebUI discovery and operations with its bearer token."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        super().__init__(app)
        self.token: str = token

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Reject unauthorized requests under the tool-server prefix."""
        if not request.url.path.startswith("/tools/"):
            return await call_next(request)
        if not _authorized(request, self.token):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)


__all__ = ["CaptureAuthMiddleware", "OpenWebUIToolAuthMiddleware"]
