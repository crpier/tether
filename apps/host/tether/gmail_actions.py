"""Gmail hygiene proposal-action executors: the first consumer of the registry.

The backlog-purge sweep (`tether.gmail_purge`) never touches the mailbox
directly — it only *proposes* typed actions, which the host executes on approval
through the executors registered here (ADR 0014). Three idempotent kinds:

- `gmail.label` — add a human-named label to a message (the name is resolved to
  its Gmail id at execute time, so a label renamed between propose and approve
  still resolves, and an unknown name fails with a clear detail).
- `gmail.archive` — remove the `INBOX` label.
- `gmail.delete` — move to Trash (a reversible soft delete, *never* a permanent
  `messages.delete`).

Every executor is fail-soft: a message already in the desired state (or already
gone) resolves `skipped`, a missing `GmailClient` or an insufficient-scope `403`
(the token predates `gmail.modify`) resolves `failed` with an actionable detail,
and neither ever crashes the host executor loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from tether.action_registry import ActionContext, ActionResult, ActionSpec
from tether.gmail import GmailApiError, GmailWriteResult

if TYPE_CHECKING:
    from tether.gmail import GmailClient

_HTTP_FORBIDDEN = 403
"""An insufficient-scope write: the cached token was minted before the
`gmail.modify` scope was added and must be re-authorized (`just gmail-auth`)."""

_NO_CLIENT_DETAIL = "gmail client unavailable"
"""Failure detail when no Gmail transport is configured on the action context."""

_SCOPE_DETAIL = (
    "gmail.modify scope missing (403): re-authorize the Gmail token by "
    "re-running `just gmail-auth` and re-consenting"
)
"""Failure detail for a `403`, telling the operator exactly how to fix it."""


class GmailLabelParams(BaseModel):
    """Params for `gmail.label`: which message, and the human label name.

    The name (not a raw Gmail id) is stored, since it is the human-meaningful
    identifier and is resolved to its id at execute time."""

    message_id: str
    label_name: str


class GmailArchiveParams(BaseModel):
    """Params for `gmail.archive`: the message to remove from the inbox."""

    message_id: str


class GmailDeleteParams(BaseModel):
    """Params for `gmail.delete`: the message to move to Trash (soft delete)."""

    message_id: str


def _to_action_result(result: GmailWriteResult) -> ActionResult:
    """Map an idempotent mailbox write onto a terminal action outcome.

    `done` succeeded; `already` / `gone` are the fail-soft `skipped` outcomes
    that let an interrupted, re-run batch resolve cleanly."""
    if result.outcome == "done":
        return ActionResult(outcome="succeeded", detail=result.detail)
    return ActionResult(outcome="skipped", detail=result.detail)


def _scope_failure(error: GmailApiError) -> ActionResult:
    """Turn a Gmail API failure into a clear `failed` outcome.

    A `403` is the insufficient-scope case (the token lacks `gmail.modify`) and
    gets the re-authorization hint; any other status is surfaced verbatim."""
    if error.status_code == _HTTP_FORBIDDEN:
        return ActionResult(outcome="failed", detail=_SCOPE_DETAIL)
    return ActionResult(outcome="failed", detail=str(error))


async def _archive(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.archive`: remove the message's `INBOX` label."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    archive_params = cast("GmailArchiveParams", params)
    try:
        result = await client.archive(archive_params.message_id)
    except GmailApiError as error:
        return _scope_failure(error)
    return _to_action_result(result)


async def _label(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.label`: resolve the label name, then add it."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    label_params = cast("GmailLabelParams", params)
    try:
        label_id = await _resolve_label(client, label_params.label_name)
        if label_id is None:
            return ActionResult(
                outcome="failed",
                detail=f"unknown Gmail label: {label_params.label_name!r}",
            )
        result = await client.label(label_params.message_id, label_id)
    except GmailApiError as error:
        return _scope_failure(error)
    return _to_action_result(result)


async def _delete(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.delete`: move the message to Trash (never permanent)."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    delete_params = cast("GmailDeleteParams", params)
    try:
        result = await client.trash(delete_params.message_id)
    except GmailApiError as error:
        return _scope_failure(error)
    return _to_action_result(result)


async def _resolve_label(client: GmailClient, name: str) -> str | None:
    """Resolve a label display name to its Gmail id, or None when unknown."""
    return await client.resolve_label_id(name)


GMAIL_ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("gmail.label", GmailLabelParams, _label, ui_hint="gmail.label"),
    ActionSpec("gmail.archive", GmailArchiveParams, _archive, ui_hint="gmail.archive"),
    ActionSpec("gmail.delete", GmailDeleteParams, _delete, ui_hint="gmail.delete"),
)
"""Every Gmail hygiene action kind, joined into `all_action_specs()`."""

__all__ = [
    "GMAIL_ACTION_SPECS",
    "GmailArchiveParams",
    "GmailDeleteParams",
    "GmailLabelParams",
]
