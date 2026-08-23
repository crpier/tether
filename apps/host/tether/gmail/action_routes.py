"""Authenticated browser routes for reversible Gmail action receipts."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import Response

from tether.capabilities import rest_response, translate_domain_errors
from tether.gmail.tools import GMAIL_TOOL_ERRORS, undo_archive


class UndoGmailActionRequest(BaseModel):
    """One completed Gmail action with a safe inverse."""

    action: Literal["archive"]
    message_id: str = Field(min_length=1)


class GmailUndoRead(BaseModel):
    """Result of applying a safe mailbox inverse."""

    detail: str | None
    message_id: str
    outcome: Literal["done", "already", "gone"]


_translate_domain_errors = translate_domain_errors(GMAIL_TOOL_ERRORS)
router = APIRouter()


@router.post("/api/gmail/actions/undo", response_model=GmailUndoRead)
@_translate_domain_errors
async def undo_gmail_action(
    request: Request,
    body: UndoGmailActionRequest,
) -> Response:
    """Undo one archive by restoring the message's inbox label."""
    return rest_response(await undo_archive(request, body.message_id))
