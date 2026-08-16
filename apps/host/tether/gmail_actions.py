"""Gmail hygiene proposal-action executors: the first consumer of the registry.

The backlog-purge sweep (`tether.gmail_purge`) never touches the mailbox
directly — it only *proposes* typed actions, which the host executes on approval
through the registered action executors. Three idempotent kinds:

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

from typing import cast

from pydantic import BaseModel
from snekok import Err

from tether.action_registry import ActionContext, ActionResult, ActionSpec
from tether.gmail_client import (
    GmailAuthenticationFailure,
    GmailFailure,
    GmailHttpFailure,
    GmailWriteResult,
)

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


def _provider_failure(error: GmailFailure) -> ActionResult:
    """Present typed provider failures as fail-soft action outcomes."""
    if (
        isinstance(error, GmailAuthenticationFailure)
        and error.status_code == _HTTP_FORBIDDEN
    ):
        return ActionResult(outcome="failed", detail=_SCOPE_DETAIL)
    if isinstance(error, GmailHttpFailure):
        detail = f"Gmail {error.operation} returned {error.status_code}"
    else:
        detail = error.message
    return ActionResult(outcome="failed", detail=detail)


async def _archive(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.archive`: remove the message's `INBOX` label."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    archive_params = cast("GmailArchiveParams", params)
    result = await client.archive(archive_params.message_id)
    if isinstance(result, Err):
        return _provider_failure(result.error)
    return _to_action_result(result.value)


async def _label(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.label`: resolve the label name, then add it."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    label_params = cast("GmailLabelParams", params)
    label_resolution = await client.resolve_label_id(label_params.label_name)
    if isinstance(label_resolution, Err):
        return _provider_failure(label_resolution.error)
    if label_resolution.value is None:
        return ActionResult(
            outcome="failed",
            detail=f"unknown Gmail label: {label_params.label_name!r}",
        )
    result = await client.label(label_params.message_id, label_resolution.value)
    if isinstance(result, Err):
        return _provider_failure(result.error)
    return _to_action_result(result.value)


async def _delete(params: BaseModel, context: ActionContext) -> ActionResult:
    """Execute `gmail.delete`: move the message to Trash (never permanent)."""
    client = context.gmail_client
    if client is None:
        return ActionResult(outcome="failed", detail=_NO_CLIENT_DETAIL)
    delete_params = cast("GmailDeleteParams", params)
    result = await client.trash(delete_params.message_id)
    if isinstance(result, Err):
        return _provider_failure(result.error)
    return _to_action_result(result.value)


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
