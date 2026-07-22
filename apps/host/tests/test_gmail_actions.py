"""Behaviour tests for the Gmail hygiene proposal-action executors.

Drive each executor (`gmail.label`, `gmail.archive`, `gmail.delete`) against a
real `GmailClient` over a scripted `GmailTransport` — no live Gmail — covering
success, an idempotent re-run (soft `skipped`), a message that has gone away, a
missing `GmailClient` on the context (soft `failed`), and an insufficient-scope
`403` (the token lacks `gmail.modify`) failing with a clear detail rather than
crashing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

import structlog
from snektest import assert_eq, assert_in, assert_true, test

from tether.action_registry import ActionContext
from tether.gmail import GmailClient, GmailResponse
from tether.gmail_actions import (
    GMAIL_ACTION_SPECS,
    GmailArchiveParams,
    GmailDeleteParams,
    GmailLabelParams,
    _archive,
    _delete,
    _label,
)
from tether.logging import Logger


def test_logger() -> Logger:
    """A throwaway structured logger."""
    return structlog.stdlib.get_logger("test.gmail_actions")


def _labels_of(payload: dict[str, object]) -> list[str]:
    raw = payload.get("labelIds")
    return [label for label in cast("list[object]", raw) if isinstance(label, str)]


def _message(message_id: str, *, label_ids: Sequence[str]) -> dict[str, object]:
    """A minimal `messages.get` payload carrying only the fields the writes read."""
    return {
        "id": message_id,
        "threadId": message_id,
        "labelIds": list(label_ids),
        "internalDate": "1700000000000",
        "payload": {"headers": [], "mimeType": "text/plain", "body": {}},
    }


@dataclass
class WriteTransport:
    """A `GmailTransport` for the write path: mutates label state, records writes."""

    messages: dict[str, dict[str, object]]
    labels: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    modify_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    )
    trash_calls: list[str] = field(default_factory=list[str])
    write_status: int = 200

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        _ = query, page_token
        return GmailResponse(status_code=200, payload={})

    async def get_message(self, message_id: str) -> GmailResponse:
        payload = self.messages.get(message_id)
        if payload is None:
            return GmailResponse(status_code=404, payload={})
        return GmailResponse(status_code=200, payload=payload)

    async def list_labels(self) -> GmailResponse:
        return GmailResponse(status_code=200, payload={"labels": self.labels})

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> GmailResponse:
        self.modify_calls.append(
            (message_id, tuple(add_label_ids), tuple(remove_label_ids))
        )
        if self.write_status != 200:
            return GmailResponse(status_code=self.write_status, payload={})
        payload = self.messages.get(message_id)
        if payload is not None:
            labels = [
                label for label in _labels_of(payload) if label not in remove_label_ids
            ]
            labels.extend(label for label in add_label_ids if label not in labels)
            payload["labelIds"] = labels
        return GmailResponse(status_code=200, payload={})

    async def trash_message(self, message_id: str) -> GmailResponse:
        self.trash_calls.append(message_id)
        if self.write_status != 200:
            return GmailResponse(status_code=self.write_status, payload={})
        payload = self.messages.get(message_id)
        if payload is not None:
            labels = _labels_of(payload)
            if "TRASH" not in labels:
                labels.append("TRASH")
            payload["labelIds"] = labels
        return GmailResponse(status_code=200, payload={})


def _context(transport: WriteTransport) -> ActionContext:
    return ActionContext(
        gmail_client=GmailClient(transport=transport), logger=test_logger()
    )


# --- Registry ---------------------------------------------------------------


@test()
async def the_registry_exposes_the_three_hygiene_kinds() -> None:
    """`GMAIL_ACTION_SPECS` covers exactly the three hygiene kinds, ui-hinted."""
    kinds = {spec.kind for spec in GMAIL_ACTION_SPECS}
    assert_eq(kinds, {"gmail.label", "gmail.archive", "gmail.delete"})
    for spec in GMAIL_ACTION_SPECS:
        assert_eq(spec.ui_hint, spec.kind)


# --- gmail.archive ----------------------------------------------------------


@test()
async def archive_succeeds_then_skips_on_rerun() -> None:
    """`gmail.archive` succeeds once, then soft-skips (already archived)."""
    transport = WriteTransport(messages={"m1": _message("m1", label_ids=["INBOX"])})
    context = _context(transport)

    first = await _archive(GmailArchiveParams(message_id="m1"), context)
    second = await _archive(GmailArchiveParams(message_id="m1"), context)

    assert_eq(first.outcome, "succeeded")
    assert_eq(second.outcome, "skipped")
    assert_eq(len(transport.modify_calls), 1)


@test()
async def archive_of_a_gone_message_is_skipped() -> None:
    """`gmail.archive` of a 404 message is a soft skip, no write."""
    transport = WriteTransport(messages={})

    result = await _archive(GmailArchiveParams(message_id="gone"), _context(transport))

    assert_eq(result.outcome, "skipped")
    assert_eq(transport.modify_calls, [])


@test()
async def archive_without_a_client_fails_soft() -> None:
    """No `GmailClient` on the context fails with a clear detail, never crashes."""
    result = await _archive(
        GmailArchiveParams(message_id="m1"),
        ActionContext(gmail_client=None, logger=test_logger()),
    )

    assert_eq(result.outcome, "failed")
    assert_in("unavailable", result.detail or "")


@test()
async def archive_with_missing_scope_fails_with_a_clear_detail() -> None:
    """A 403 (token lacks gmail.modify) fails with a re-authorization hint."""
    transport = WriteTransport(
        messages={"m1": _message("m1", label_ids=["INBOX"])}, write_status=403
    )

    result = await _archive(GmailArchiveParams(message_id="m1"), _context(transport))

    assert_eq(result.outcome, "failed")
    assert_in("gmail.modify", result.detail or "")
    assert_in("403", result.detail or "")


# --- gmail.label ------------------------------------------------------------


@test()
async def label_resolves_the_name_then_adds_it() -> None:
    """`gmail.label` resolves the label name to its id and adds it; re-run skips."""
    transport = WriteTransport(
        messages={"m1": _message("m1", label_ids=["INBOX"])},
        labels=[{"id": "Label_7", "name": "receipts"}],
    )
    context = _context(transport)

    first = await _label(
        GmailLabelParams(message_id="m1", label_name="receipts"), context
    )
    second = await _label(
        GmailLabelParams(message_id="m1", label_name="receipts"), context
    )

    assert_eq(first.outcome, "succeeded")
    assert_eq(second.outcome, "skipped")
    assert_eq(transport.modify_calls[0], ("m1", ("Label_7",), ()))


@test()
async def label_with_an_unknown_name_fails_clearly() -> None:
    """An unresolvable label name fails with the name in the detail, no write."""
    transport = WriteTransport(messages={"m1": _message("m1", label_ids=["INBOX"])})

    result = await _label(
        GmailLabelParams(message_id="m1", label_name="nope"), _context(transport)
    )

    assert_eq(result.outcome, "failed")
    assert_in("nope", result.detail or "")
    assert_eq(transport.modify_calls, [])


@test()
async def label_of_a_gone_message_is_skipped() -> None:
    """`gmail.label` of a 404 message is a soft skip."""
    transport = WriteTransport(
        messages={}, labels=[{"id": "Label_7", "name": "receipts"}]
    )

    result = await _label(
        GmailLabelParams(message_id="gone", label_name="receipts"), _context(transport)
    )

    assert_eq(result.outcome, "skipped")
    assert_eq(transport.modify_calls, [])


# --- gmail.delete (move to trash) -------------------------------------------


@test()
async def delete_moves_to_trash_then_skips_on_rerun() -> None:
    """`gmail.delete` trashes once, then soft-skips (already trashed)."""
    transport = WriteTransport(messages={"m1": _message("m1", label_ids=["INBOX"])})
    context = _context(transport)

    first = await _delete(GmailDeleteParams(message_id="m1"), context)
    second = await _delete(GmailDeleteParams(message_id="m1"), context)

    assert_eq(first.outcome, "succeeded")
    assert_eq(second.outcome, "skipped")
    assert_eq(transport.trash_calls, ["m1"])


@test()
async def delete_of_a_gone_message_is_skipped() -> None:
    """`gmail.delete` of a 404 message is a soft skip, no trash call."""
    transport = WriteTransport(messages={})

    result = await _delete(GmailDeleteParams(message_id="gone"), _context(transport))

    assert_eq(result.outcome, "skipped")
    assert_eq(transport.trash_calls, [])


@test()
async def delete_without_a_client_fails_soft() -> None:
    """No `GmailClient` fails soft for delete too."""
    result = await _delete(
        GmailDeleteParams(message_id="m1"),
        ActionContext(gmail_client=None, logger=test_logger()),
    )

    assert_eq(result.outcome, "failed")
    assert_true("unavailable" in (result.detail or ""))
