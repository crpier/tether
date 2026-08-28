"""Authenticated upload and read surfaces for Conversation attachments."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from snekql.sqlite import Fetched
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from tether.app_runtime import app_runtime
from tether.attachment_store import AttachmentKind, MessageAttachment
from tether.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    AttachmentValidationError,
)
from tether.conversation_model import ConversationNotFoundError


@dataclass(frozen=True, slots=True)
class _AttachmentUpload:
    """One bounded multipart file translated out of HTTP types."""

    content: bytes
    declared_mime_type: str
    filename: str


async def _read_attachment_upload(
    request: Request,
) -> _AttachmentUpload | JSONResponse:
    """Parse one bounded multipart file or return its stable input failure."""
    try:
        form = await request.form(max_part_size=MAX_ATTACHMENT_BYTES + 1)
    except MultiPartException:
        return JSONResponse({"detail": "malformed multipart upload"}, status_code=400)
    file_part = form.get("file")
    if not isinstance(file_part, UploadFile):
        return JSONResponse(
            {"detail": "a multipart 'file' part is required"}, status_code=422
        )
    if file_part.size is not None and file_part.size > MAX_ATTACHMENT_BYTES:
        await file_part.close()
        return JSONResponse(
            {"detail": "attachment exceeds the 10 MB size limit"},
            status_code=413,
        )
    upload = _AttachmentUpload(
        content=await file_part.read(MAX_ATTACHMENT_BYTES + 1),
        declared_mime_type=file_part.content_type or "application/octet-stream",
        filename=file_part.filename or "attachment",
    )
    await file_part.close()
    return upload


class AttachmentRead(BaseModel):
    """Browser representation of one immutable Message attachment."""

    filename: str
    id: UUID
    kind: AttachmentKind
    mime_type: str
    size_bytes: int

    @classmethod
    def from_attachment(cls, attachment: MessageAttachment[Fetched]) -> AttachmentRead:
        """Render stored metadata without exposing its filesystem location."""
        return cls(
            filename=attachment.filename,
            id=attachment.id,
            kind=attachment.kind,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
        )


router = APIRouter()


@router.get("/api/attachments/{attachment_id}")
async def download_attachment(request: Request, attachment_id: UUID) -> Response:
    """Download one immutable attachment through browser authentication."""
    runtime = app_runtime(request.app)
    try:
        attachment = await runtime.attachment_service.fetch(attachment_id)
    except AttachmentNotFoundError:
        return JSONResponse({"detail": "attachment not found"}, status_code=404)
    return FileResponse(
        runtime.attachment_service.storage_root / str(attachment.id),
        filename=attachment.filename,
        headers={"X-Content-Type-Options": "nosniff"},
        media_type=attachment.mime_type,
    )


@router.post(
    "/api/conversations/{conversation_id}/attachments",
    response_model=AttachmentRead,
    status_code=201,
)
async def upload_attachment(request: Request, conversation_id: UUID) -> Response:
    """Stage one validated immutable file for a later Conversation turn."""
    upload = await _read_attachment_upload(request)
    if isinstance(upload, JSONResponse):
        return upload
    try:
        attachment = await app_runtime(request.app).attachment_service.create(
            conversation_id,
            content=upload.content,
            declared_mime_type=upload.declared_mime_type,
            filename=upload.filename,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except AttachmentTooLargeError as error:
        return JSONResponse({"detail": str(error)}, status_code=413)
    except AttachmentValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return JSONResponse(
        AttachmentRead.from_attachment(attachment).model_dump(mode="json"),
        status_code=201,
    )


__all__ = ["AttachmentRead", "router"]
