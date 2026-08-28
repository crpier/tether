"""Explicit promotion of foreground Gmail reads into citeable Evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import Parser
from typing import Protocol
from uuid import UUID

from pydantic import UUID7, PositiveInt
from snekql.sqlite import Database, Fetched, insert, select

from tether.conversation_store import Message
from tether.email_evidence_store import (
    EmailEvidencePromotion,
    EmailEvidenceSnapshot,
)

_EMAIL_BODY_MAX_CHARS = 50_000
"""Maximum parsed source text retained by one promoted email snapshot."""

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_WHITESPACE = re.compile(r"\s+")


class _RawEmailSource(Protocol):
    """Host-read source fields accepted from an email Integration."""

    @property
    def message_id(self) -> str: ...

    @property
    def raw_rfc2822(self) -> str: ...

    @property
    def thread_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class EmailEvidence:
    """One exact immutable email source exposed to citation readers."""

    body_chars: int
    body_text: str
    body_truncated: bool
    captured_at: datetime
    content_hash: str
    date_header: str
    from_header: str
    gmail_message_id: str
    snapshot_id: UUID7
    subject: str
    thread_id: str
    uri: str


@dataclass(frozen=True, slots=True)
class EmailDreamEvidence:
    """One promoted source selected by a bounded Conversation window."""

    authorizing_message_seq: PositiveInt
    body_text: str
    captured_at: datetime
    claim_hint: str
    date_header: str
    from_header: str
    subject: str
    uri: str


@dataclass(frozen=True, slots=True)
class PromotedEmailEvidence:
    """Stable identity returned after an authorized source promotion."""

    message_id: str
    snapshot_id: UUID7
    uri: str


class EmailEvidenceService:
    """Own immutable email snapshots and their foreground nominations.

    ```python
    service = EmailEvidenceService(database)
    ```
    """

    def __init__(self, database: Database) -> None:
        self._database: Database = database

    async def resolve(self, snapshot_id: UUID, *, uri: str) -> EmailEvidence | None:
        """Resolve one local snapshot without consulting the remote mailbox."""
        async with self._database.transaction() as transaction:
            snapshot = await transaction.fetch_one_or_none(
                select(EmailEvidenceSnapshot).where(
                    EmailEvidenceSnapshot.id.eq(snapshot_id)
                )
            )
        if snapshot is None:
            return None
        return EmailEvidence(
            body_chars=snapshot.body_chars,
            body_text=snapshot.body_text,
            body_truncated=bool(snapshot.body_truncated),
            captured_at=snapshot.captured_at,
            content_hash=snapshot.content_hash,
            date_header=snapshot.date_header,
            from_header=snapshot.from_header,
            gmail_message_id=snapshot.gmail_message_id,
            snapshot_id=snapshot.id,
            subject=snapshot.subject,
            thread_id=snapshot.thread_id,
            uri=uri,
        )

    async def fetch_for_conversation_window(
        self,
        conversation_id: UUID,
        *,
        start_seq: int,
        end_seq: int,
    ) -> list[EmailDreamEvidence]:
        """Fetch promoted sources authorized inside exact Message bounds."""
        async with self._database.transaction() as transaction:
            promotions = await transaction.fetch_all(
                select(EmailEvidencePromotion)
                .where(
                    EmailEvidencePromotion.authorizing_conversation_id.eq(
                        conversation_id
                    )
                )
                .where(EmailEvidencePromotion.authorizing_message_seq.gte(start_seq))
                .where(EmailEvidencePromotion.authorizing_message_seq.lte(end_seq))
                .order_by(EmailEvidencePromotion.created_at.asc())
            )
            sources: list[EmailDreamEvidence] = []
            for promotion in promotions:
                snapshot = await transaction.fetch_one_or_none(
                    select(EmailEvidenceSnapshot).where(
                        EmailEvidenceSnapshot.id.eq(promotion.snapshot_id)
                    )
                )
                if snapshot is None:
                    continue
                sources.append(
                    EmailDreamEvidence(
                        authorizing_message_seq=promotion.authorizing_message_seq,
                        body_text=snapshot.body_text,
                        captured_at=snapshot.captured_at,
                        claim_hint=promotion.claim_hint,
                        date_header=snapshot.date_header,
                        from_header=snapshot.from_header,
                        subject=snapshot.subject,
                        uri=f"tether://email/{snapshot.id}",
                    )
                )
        return sources

    async def promote(
        self,
        source: _RawEmailSource,
        *,
        authorizing_message: Message[Fetched],
        claim_hint: str,
    ) -> PromotedEmailEvidence:
        """Snapshot one host-read source and link it to fresh user Evidence."""
        content_hash = hashlib.sha256(source.raw_rfc2822.encode()).hexdigest()
        parsed = Parser(policy=policy.default).parsestr(source.raw_rfc2822)
        body_part = parsed.get_body(preferencelist=("plain", "html"))
        body_content = (
            source.raw_rfc2822 if body_part is None else body_part.get_content()
        )
        body_text = (
            body_content
            if isinstance(body_content, str)
            else body_content.decode("utf-8", errors="replace")
        )
        if body_part is not None and body_part.get_content_subtype() == "html":
            body_text = _HTML_WHITESPACE.sub(" ", _HTML_TAG.sub(" ", body_text))
        body_chars = len(body_text)
        bounded_body_text = body_text[:_EMAIL_BODY_MAX_CHARS]

        async with self._database.transaction(mode="immediate") as transaction:
            snapshot = await transaction.fetch_one_or_none(
                select(EmailEvidenceSnapshot)
                .where(EmailEvidenceSnapshot.gmail_message_id.eq(source.message_id))
                .where(EmailEvidenceSnapshot.content_hash.eq(content_hash))
            )
            if snapshot is None:
                snapshot = await transaction.execute(
                    insert(
                        EmailEvidenceSnapshot(
                            body_chars=body_chars,
                            body_text=bounded_body_text,
                            body_truncated=body_chars > len(bounded_body_text),
                            content_hash=content_hash,
                            date_header=str(parsed.get("Date", "")),
                            from_header=str(parsed.get("From", "")),
                            gmail_message_id=source.message_id,
                            subject=str(parsed.get("Subject", "")),
                            thread_id=source.thread_id,
                        )
                    ).returning()
                )
            promotion = await transaction.fetch_one_or_none(
                select(EmailEvidencePromotion)
                .where(EmailEvidencePromotion.snapshot_id.eq(snapshot.id))
                .where(
                    EmailEvidencePromotion.authorizing_message_id.eq(
                        authorizing_message.id
                    )
                )
                .where(EmailEvidencePromotion.claim_hint.eq(claim_hint))
            )
            if promotion is None:
                _ = await transaction.execute(
                    insert(
                        EmailEvidencePromotion(
                            authorizing_conversation_id=authorizing_message.conversation_id,
                            authorizing_message_id=authorizing_message.id,
                            authorizing_message_seq=authorizing_message.seq,
                            claim_hint=claim_hint,
                            snapshot_id=snapshot.id,
                        )
                    )
                )
        return PromotedEmailEvidence(
            message_id=source.message_id,
            snapshot_id=snapshot.id,
            uri=f"tether://email/{snapshot.id}",
        )


__all__ = [
    "EmailDreamEvidence",
    "EmailEvidence",
    "EmailEvidenceService",
    "PromotedEmailEvidence",
]
