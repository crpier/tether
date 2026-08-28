"""Read-only compatibility for retained historical email Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import UUID7
from snekql.sqlite import Database, select

from tether.email_evidence_store import EmailEvidenceSnapshot


@dataclass(frozen=True, slots=True)
class EmailEvidence:
    """One retained immutable email source exposed to citation readers."""

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


class EmailEvidenceService:
    """Resolve historical snapshots without creating new email Evidence.

    ```python
    service = EmailEvidenceService(database)
    ```
    """

    def __init__(self, database: Database) -> None:
        self._database: Database = database

    async def resolve(self, snapshot_id: UUID, *, uri: str) -> EmailEvidence | None:
        """Resolve one retained snapshot without consulting the remote mailbox."""
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


__all__ = ["EmailEvidence", "EmailEvidenceService"]
