"""HTTP routes for the kosync gate: the device protocol plus ebook labeling.

Two surfaces live here.

- The **kosync protocol** (`kosync_protocol_routes`) is what KOReader devices
  speak: five endpoints mounted under `/kosync`, outside the `/api/*` app-session
  gate, authenticated instead by the device's own `x-auth-user` / `x-auth-key`
  headers against the single pre-provisioned user from settings. Registration is
  always refused (single-tenant). These are declared as plain Starlette routes,
  not through the OpenAPI contract layer, because their request/response shapes
  are fixed by the kosync protocol, not by Tether — and they are mounted only
  when the gate is configured, so a disabled install answers 404.
- The **labeling** REST routes (`ebook_routes`) are the owner-facing side of the
  `tether.kosync_capabilities` descriptor, the REST twin of the internal tools,
  and ride the normal `/api/*` session-gated contract layer.

Progress error bodies follow the kosync convention exactly (verified against the
reference koreader-sync-server): `{"code": <int>, "message": <str>}` with the
matching HTTP status.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether import kosync_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.kosync import KosyncService, ProgressUpdate
from tether.kosync_capabilities import KOSYNC_ERRORS, EbookDocumentRead
from tether.logging import get_request_logger
from tether.openapi import EndpointRoute, endpoint

_CODE_UNAUTHORIZED = 2001
_CODE_INVALID_REQUEST = 2003
_CODE_DOCUMENT_MISSING = 2004
_CODE_REGISTRATION_DISABLED = 2005


@dataclass(frozen=True, slots=True)
class KosyncAuth:
    """The single pre-provisioned kosync user a device authenticates as.

    `userkey` is the `md5(password)` string the device sends verbatim in
    `x-auth-key`; Tether compares it constant-time, never hashing anything
    itself.
    """

    username: str
    userkey: str


def _kosync_error(code: int, message: str, status_code: int) -> JSONResponse:
    """Render a kosync error body: `{code, message}` at the matching status."""
    return JSONResponse({"code": code, "message": message}, status_code=status_code)


def _authorized(request: Request) -> bool:
    """Whether the request carries the configured `x-auth-user`/`x-auth-key`.

    Both header comparisons are constant-time; a device is authorised only when
    both the username and the pre-hashed key match the pre-provisioned user.
    """
    auth = cast("KosyncAuth", request.app.state.kosync_auth)
    offered_user = request.headers.get("x-auth-user", "")
    offered_key = request.headers.get("x-auth-key", "")
    return hmac.compare_digest(offered_user, auth.username) and hmac.compare_digest(
        offered_key, auth.userkey
    )


def _service(request: Request) -> KosyncService:
    """The `KosyncService` wired onto app state for the whole process."""
    return cast("KosyncService", request.app.state.kosync_service)


async def create_user(_request: Request) -> Response:
    """Refuse registration: Tether's kosync user is pre-provisioned from settings."""
    return _kosync_error(
        _CODE_REGISTRATION_DISABLED,
        "User registration is disabled.",
        status_code=402,
    )


async def authorize_user(request: Request) -> Response:
    """Confirm the device's credentials, the check KOReader runs before syncing."""
    if not _authorized(request):
        return _kosync_error(_CODE_UNAUTHORIZED, "Unauthorized", status_code=401)
    return JSONResponse({"authorized": "OK"})


async def put_progress(request: Request) -> Response:
    """Store one progress push and echo the server timestamp back to the device.

    Field validation mirrors the reference server exactly: a missing or empty
    `document` is its own error (`2004`), any other missing required field is
    the generic invalid request (`2003`). `device_id` is optional and defaults
    to the empty string.
    """
    if not _authorized(request):
        return _kosync_error(_CODE_UNAUTHORIZED, "Unauthorized", status_code=401)
    body = await _json_object(request)
    document = body.get("document")
    if not isinstance(document, str) or not document:
        return _kosync_error(
            _CODE_DOCUMENT_MISSING,
            "Field 'document' not provided.",
            status_code=403,
        )
    update = _progress_update(document, body)
    if update is None:
        return _kosync_error(_CODE_INVALID_REQUEST, "Invalid request", status_code=403)
    server_timestamp = await _service(request).record_progress(
        update, logger=get_request_logger(request), now=datetime.now(UTC)
    )
    return JSONResponse({"document": document, "timestamp": server_timestamp})


async def get_progress(request: Request) -> Response:
    """Return the furthest stored progress for a document, or `{}` when none."""
    if not _authorized(request):
        return _kosync_error(_CODE_UNAUTHORIZED, "Unauthorized", status_code=401)
    latest = await _service(request).latest_progress(request.path_params["document"])
    if latest is None:
        return JSONResponse({})
    return JSONResponse(
        {
            "document": latest.document,
            "percentage": latest.percentage,
            "progress": latest.progress,
            "device": latest.device,
            "device_id": latest.device_id,
            "timestamp": latest.timestamp,
        }
    )


async def healthcheck(_request: Request) -> Response:
    """Report liveness with no auth, as KOReader's server probe expects."""
    return JSONResponse({"state": "OK"})


async def _json_object(request: Request) -> dict[str, object]:
    """Decode a JSON object body, treating any non-object as empty.

    A malformed body then fails the field checks and surfaces as the protocol's
    own `2003`/`2004` errors rather than a bare 400 the device cannot read.
    """
    try:
        decoded: object = await request.json()
    except ValueError:
        return {}
    return cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}


def _progress_update(document: str, body: dict[str, object]) -> ProgressUpdate | None:
    """Build a `ProgressUpdate` from a body, or None when a field is unusable.

    `percentage` must be a number, `progress`/`device` non-empty strings;
    `device_id` is optional. A bool is rejected as a percentage — `True` is an
    `int` subclass but never a valid reading fraction.
    """
    percentage = body.get("percentage")
    progress = body.get("progress")
    device = body.get("device")
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
        return None
    if not isinstance(progress, str) or not progress:
        return None
    if not isinstance(device, str) or not device:
        return None
    device_id = body.get("device_id")
    return ProgressUpdate(
        document=document,
        percentage=float(percentage),
        progress=progress,
        device=device,
        device_id=device_id if isinstance(device_id, str) else "",
    )


def kosync_protocol_routes() -> list[Route]:
    """The five device-facing kosync endpoints, mounted under `/kosync`.

    Mounted only when the gate is configured, so a disabled install leaves the
    whole prefix unhandled (404). Deliberately absent from the OpenAPI document:
    the shapes are the kosync protocol's, not Tether's public API.
    """
    return [
        Route("/kosync/users/create", create_user, methods=["POST"]),
        Route("/kosync/users/auth", authorize_user, methods=["GET"]),
        Route("/kosync/syncs/progress", put_progress, methods=["PUT"]),
        Route("/kosync/syncs/progress/{document}", get_progress, methods=["GET"]),
        Route("/kosync/healthcheck", healthcheck, methods=["GET"]),
    ]


class LabelEbookRequest(BaseModel):
    """Body for attaching a human title to a document hash."""

    document_hash: str = Field(min_length=1)
    title: str = Field(min_length=1)


class MatchEbookFilenameRequest(BaseModel):
    """Body for labeling the document a filename hashes to."""

    filename: str = Field(min_length=1)


_translate_domain_errors = translate_domain_errors(KOSYNC_ERRORS)


@endpoint(response=EbookDocumentRead, response_is_list=True)
async def list_unlabeled_ebooks(request: Request) -> Response:
    """List every ebook Tether has progress for that is still unlabeled."""
    return rest_response(await kosync_capabilities.list_unlabeled_ebooks(request))


@endpoint(request_body=LabelEbookRequest, response=EbookDocumentRead)
@_translate_domain_errors
async def label_ebook(request: Request, body: LabelEbookRequest) -> Response:
    """Attach a human title to a document hash."""
    return rest_response(
        await kosync_capabilities.label_ebook(request, body.document_hash, body.title)
    )


@endpoint(request_body=MatchEbookFilenameRequest, response=EbookDocumentRead)
@_translate_domain_errors
async def match_ebook_filename(
    request: Request, body: MatchEbookFilenameRequest
) -> Response:
    """Label the document a filename hashes to, titled from the filename."""
    return rest_response(
        await kosync_capabilities.match_ebook_filename(request, body.filename)
    )


ebook_routes: list[Route] = [
    EndpointRoute("/api/ebooks/unlabeled", list_unlabeled_ebooks, methods=["GET"]),
    EndpointRoute("/api/ebooks/label", label_ebook, methods=["POST"]),
    EndpointRoute("/api/ebooks/match-filename", match_ebook_filename, methods=["POST"]),
]
"""The owner-facing ebook-labeling REST routes, the twin of the internal tools."""
