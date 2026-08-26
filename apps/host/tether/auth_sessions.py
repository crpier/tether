"""Credential verification and stateless signed app-session cookies."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from itsdangerous import BadData, URLSafeSerializer
from starlette.responses import Response

from tether.auth_model import Principal

SESSION_COOKIE = "tether_session"
"""Browser cookie name carrying the signed app session token."""

_SESSION_SUBJECT = "app"
_SESSION_TTL = timedelta(days=30)
_BEARER_PREFIX = "Bearer "


def _session_serializer(session_secret: str) -> URLSafeSerializer:
    """Create the signer used for stateless app session cookies."""
    return URLSafeSerializer(session_secret, salt="tether-session")


def authenticate_password(password: str, configured_password: str) -> Principal | None:
    """Validate the single app password in constant time."""
    if secrets.compare_digest(password.encode(), configured_password.encode()):
        return Principal(sub=_SESSION_SUBJECT)
    return None


def authenticate_bearer_token(
    authorization_header: str, configured_token: str
) -> Principal | None:
    """Validate an opted-in static mobile bearer token in constant time."""
    if not configured_token or not authorization_header.startswith(_BEARER_PREFIX):
        return None
    presented = authorization_header[len(_BEARER_PREFIX) :]
    if secrets.compare_digest(presented.encode(), configured_token.encode()):
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
    """Return the principal from a valid, current signed cookie."""
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
