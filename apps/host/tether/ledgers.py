"""User-approved generic Ledger definitions and immutable records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.ledger_errors import (
    InvalidLedgerError,
    LedgerConflictError,
    LedgerFieldValueError,
    LedgerNotFoundError,
)
from tether.ledger_model import (
    LedgerDefinition,
    LedgerEntryDraft,
    LedgerEntryQuery,
    LedgerFieldDefinition,
    LedgerFieldType,
    LedgerScalarValue,
)
from tether.ledger_store import Ledger, LedgerEntry, LedgerProposal, LedgerRevision

_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_MAX_DECIMAL_CHARS = 64
_MAX_TEXT_BYTES = 4_096

type _FieldValueValidator = Callable[
    [LedgerFieldDefinition, LedgerScalarValue],
    None,
]


def _validate_boolean(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    if not isinstance(field_value, bool):
        raise LedgerFieldValueError(field.field_id, "requires boolean")


def _validate_integer(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    if isinstance(field_value, bool) or not isinstance(field_value, int):
        raise LedgerFieldValueError(field.field_id, "requires integer")


def _validated_string(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> str:
    if not isinstance(field_value, str):
        raise LedgerFieldValueError(field.field_id, "requires text input")
    return field_value


def _validate_text(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    if len(_validated_string(field, field_value).encode()) > _MAX_TEXT_BYTES:
        raise LedgerFieldValueError(field.field_id, "is too large")


def _validate_enum(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    if _validated_string(field, field_value) not in (field.enum_values or []):
        raise LedgerFieldValueError(field.field_id, "has an unsupported enum value")


def _validate_decimal(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    decimal_text = _validated_string(field, field_value)
    if (
        _DECIMAL_PATTERN.fullmatch(decimal_text) is None
        or len(decimal_text) > _MAX_DECIMAL_CHARS
    ):
        raise LedgerFieldValueError(field.field_id, "requires a plain decimal string")
    try:
        parsed_decimal = Decimal(decimal_text)
    except InvalidOperation as error:
        raise LedgerFieldValueError(
            field.field_id,
            "requires a finite decimal",
        ) from error
    if not parsed_decimal.is_finite():
        raise LedgerFieldValueError(field.field_id, "requires a finite decimal")


def _validate_date(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    date_text = _validated_string(field, field_value)
    try:
        parsed_date = date.fromisoformat(date_text)
    except ValueError as error:
        raise LedgerFieldValueError(field.field_id, "requires an ISO date") from error
    if parsed_date.isoformat() != date_text:
        raise LedgerFieldValueError(field.field_id, "requires an ISO date")


def _validate_datetime(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    datetime_text = _validated_string(field, field_value)
    try:
        parsed_datetime = datetime.fromisoformat(datetime_text)
    except ValueError as error:
        raise LedgerFieldValueError(
            field.field_id,
            "requires an aware ISO datetime",
        ) from error
    if parsed_datetime.tzinfo is None:
        raise LedgerFieldValueError(
            field.field_id,
            "requires an aware ISO datetime",
        )


_FIELD_VALUE_VALIDATORS: dict[LedgerFieldType, _FieldValueValidator] = {
    "boolean": _validate_boolean,
    "date": _validate_date,
    "datetime": _validate_datetime,
    "decimal": _validate_decimal,
    "enum": _validate_enum,
    "integer": _validate_integer,
    "text": _validate_text,
}


def _validate_field_value(
    field: LedgerFieldDefinition,
    field_value: LedgerScalarValue,
) -> None:
    """Apply the closed scalar validator selected by the approved schema."""
    _FIELD_VALUE_VALIDATORS[field.type](field, field_value)


def _validated_entry_values(
    revision: LedgerRevision[Fetched],
    draft: LedgerEntryDraft,
) -> dict[str, LedgerScalarValue]:
    """Validate one complete entry before any member of its batch mutates state."""
    fields = {field.field_id: field for field in revision.fields}
    supplied = set(draft.values)
    unknown = supplied - fields.keys()
    if unknown:
        message = f"Unknown Ledger field: {sorted(unknown)[0]}"
        raise InvalidLedgerError(message)
    deprecated = {field_id for field_id in supplied if fields[field_id].deprecated}
    if deprecated:
        message = f"Deprecated Ledger field: {sorted(deprecated)[0]}"
        raise InvalidLedgerError(message)
    missing = {
        field.field_id
        for field in revision.fields
        if field.required and not field.deprecated and field.field_id not in supplied
    }
    if missing:
        message = f"Missing required Ledger field: {sorted(missing)[0]}"
        raise InvalidLedgerError(message)
    for field_id, field_value in draft.values.items():
        _validate_field_value(fields[field_id], field_value)
    return dict(draft.values)


def _entry_dedupe_key(
    *,
    batch_index: int,
    draft: LedgerEntryDraft,
    evidence: LedgerUserEvidence,
    ledger_id: UUID,
    revision: PositiveInt,
) -> str:
    """Identify a repeated write from the same Evidence and exact record content."""
    canonical = json.dumps(
        {
            "batch_index": batch_index,
            "ledger_id": str(ledger_id),
            "occurred_at": draft.occurred_at.isoformat()
            if draft.occurred_at is not None
            else None,
            "revision": revision,
            "source_message_id": str(evidence.message_id),
            "supersedes_entry_id": str(draft.supersedes_entry_id)
            if draft.supersedes_entry_id is not None
            else None,
            "values": draft.values,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _entry_matches_query(
    entry: LedgerEntry[Fetched],
    query: LedgerEntryQuery,
    superseder_by_target: dict[UUID7, UUID7],
) -> bool:
    """Apply bounded current-state, time, field, and lexical filters."""
    if not query.include_superseded and entry.id in superseder_by_target:
        return False
    effective_time = entry.occurred_at or entry.recorded_at
    if query.after is not None and effective_time < query.after:
        return False
    if query.before is not None and effective_time >= query.before:
        return False
    if query.field_equals and any(
        entry.values.get(field_id) != expected
        for field_id, expected in query.field_equals.items()
    ):
        return False
    searchable = " ".join(str(value) for value in entry.values.values())
    return all(term in searchable.casefold() for term in query.q.casefold().split())


@dataclass(frozen=True, slots=True)
class _PlannedLedgerEntry:
    """One validated append candidate before its batch mutates state."""

    dedupe_key: str
    draft: LedgerEntryDraft
    existing: LedgerEntry[Fetched] | None
    values: dict[str, LedgerScalarValue]


@dataclass(frozen=True, slots=True)
class LedgerUserEvidence:
    """The exact interactive user Message authorizing a Ledger mutation."""

    conversation_id: UUID7
    message_id: UUID7


@dataclass(frozen=True, slots=True)
class LedgerEntryView:
    """One entry projected with its current supersession state."""

    entry: LedgerEntry[Fetched]
    superseded_by_entry_id: UUID7 | None


@dataclass(frozen=True, slots=True)
class LedgerExportSnapshot:
    """One transactionally consistent complete Ledger export."""

    entries: list[LedgerEntryView]
    ledger_id: UUID7
    proposals: list[LedgerProposal[Fetched]]
    revisions: list[LedgerRevision[Fetched]]


class LedgerService:
    """Own Ledger proposal, approval, revision, entry, and query policy.

    ```python
    service = LedgerService(database)
    proposal = await service.propose(
        definition,
        evidence=LedgerUserEvidence(conversation_id, message_id),
    )
    assert proposal.status == "pending"
    ```
    """

    def __init__(
        self,
        database: Database,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def append_entries(
        self,
        ledger_id: UUID7,
        revision: PositiveInt,
        drafts: list[LedgerEntryDraft],
        *,
        evidence: LedgerUserEvidence,
    ) -> list[LedgerEntry[Fetched]]:
        """Atomically append one bounded batch under the current active schema."""
        async with self.database.transaction(mode="immediate") as transaction:
            current = await self._fetch_append_revision(
                transaction,
                ledger_id,
                revision,
            )
            planned = await self._plan_entries(
                transaction,
                current,
                drafts,
                evidence=evidence,
            )
            await self._validate_correction_targets(
                transaction,
                ledger_id,
                planned,
            )
            entries = await self._append_planned_entries(
                transaction,
                ledger_id,
                current.revision,
                planned,
                evidence=evidence,
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["ledgers"]))
        return entries

    async def propose(
        self,
        definition: LedgerDefinition,
        *,
        evidence: LedgerUserEvidence,
    ) -> LedgerProposal[Fetched]:
        """Freeze one new Ledger definition for later user approval."""
        if not definition.name.strip() or not definition.purpose.strip():
            message = "Ledger name and purpose must not be blank"
            raise InvalidLedgerError(message)
        async with self.database.transaction(mode="immediate") as transaction:
            proposal = await transaction.execute(
                insert(
                    LedgerProposal(
                        fields=definition.fields,
                        kind="create",
                        ledger_status=definition.status,
                        name=definition.name.strip(),
                        proposed_by_conversation_id=evidence.conversation_id,
                        proposed_by_message_id=evidence.message_id,
                        proposed_revision=1,
                        purpose=definition.purpose.strip(),
                        status="pending",
                    )
                ).returning()
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["ledgers"]))
        return proposal

    async def propose_revision(
        self,
        ledger_id: UUID7,
        revision: PositiveInt,
        definition: LedgerDefinition,
        *,
        evidence: LedgerUserEvidence,
    ) -> LedgerProposal[Fetched]:
        """Freeze one complete successor definition against current state."""
        async with self.database.transaction(mode="immediate") as transaction:
            current = await transaction.fetch_one_or_none(
                select(LedgerRevision)
                .where(LedgerRevision.ledger_id.eq(ledger_id))
                .order_by(LedgerRevision.revision.desc())
                .limit(1)
            )
            if current is None:
                raise LedgerNotFoundError(str(ledger_id))
            if current.revision != revision:
                message = (
                    f"Ledger {ledger_id} changed from revision {revision} "
                    f"to {current.revision}"
                )
                raise LedgerConflictError(message)
            if current.status != "active":
                message = "Completed or abandoned Ledgers are terminal"
                raise LedgerConflictError(message)
            current_fields = {field.field_id: field for field in current.fields}
            proposed_fields = {field.field_id: field for field in definition.fields}
            removed = current_fields.keys() - proposed_fields.keys()
            if removed:
                message = "Ledger revisions deprecate fields instead of removing them"
                raise InvalidLedgerError(message)
            for field_id, prior in current_fields.items():
                successor = proposed_fields[field_id]
                if (
                    successor.type != prior.type
                    or successor.description != prior.description
                    or successor.unit != prior.unit
                ):
                    message = f"Ledger field {field_id} changed type or meaning"
                    raise InvalidLedgerError(message)
                if prior.type == "enum" and not set(prior.enum_values or []).issubset(
                    successor.enum_values or []
                ):
                    message = f"Ledger enum field {field_id} removed a value"
                    raise InvalidLedgerError(message)
            proposal = await transaction.execute(
                insert(
                    LedgerProposal(
                        base_revision=current.revision,
                        fields=definition.fields,
                        kind="revise",
                        ledger_id=current.ledger_id,
                        ledger_status=definition.status,
                        name=definition.name.strip(),
                        proposed_by_conversation_id=evidence.conversation_id,
                        proposed_by_message_id=evidence.message_id,
                        proposed_revision=current.revision + 1,
                        purpose=definition.purpose.strip(),
                        status="pending",
                    )
                ).returning()
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["ledgers"]))
        return proposal

    async def approve_proposal(
        self,
        proposal_id: UUID7,
        *,
        evidence: LedgerUserEvidence,
    ) -> LedgerRevision[Fetched]:
        """Approve the frozen proposal from a distinct later user Message."""
        async with self.database.transaction(mode="immediate") as transaction:
            proposal = await transaction.fetch_one_or_none(
                select(LedgerProposal).where(LedgerProposal.id.eq(proposal_id))
            )
            if proposal is None:
                raise LedgerNotFoundError(str(proposal_id))
            if proposal.status != "pending":
                message = "Ledger proposal is already approved"
                raise LedgerConflictError(message)
            if proposal.proposed_by_message_id == evidence.message_id:
                message = "Ledger approval requires a later interactive user Message"
                raise InvalidLedgerError(message)
            if proposal.kind == "revise":
                current = await transaction.fetch_one_or_none(
                    select(LedgerRevision)
                    .where(LedgerRevision.ledger_id.eq(proposal.ledger_id))
                    .order_by(LedgerRevision.revision.desc())
                    .limit(1)
                )
                if current is None:
                    raise LedgerNotFoundError(str(proposal.ledger_id))
                if current.revision != proposal.base_revision:
                    message = "Ledger changed after this revision was proposed"
                    raise LedgerConflictError(message)
            matched = await transaction.execute(
                update(LedgerProposal)
                .set(LedgerProposal.status.to("approved"))
                .set(LedgerProposal.approved_at.to(CurrentTimestamp))
                .set(LedgerProposal.approved_by_message_id.to(evidence.message_id))
                .where(LedgerProposal.id.eq(proposal.id))
                .where(LedgerProposal.status.eq("pending"))
            )
            if matched == 0:
                message = "Ledger proposal changed before approval"
                raise LedgerConflictError(message)
            if proposal.kind == "create":
                _ = await transaction.execute(insert(Ledger(id=proposal.ledger_id)))
            revision = await transaction.execute(
                insert(
                    LedgerRevision(
                        approved_by_conversation_id=evidence.conversation_id,
                        approved_by_message_id=evidence.message_id,
                        fields=proposal.fields,
                        ledger_id=proposal.ledger_id,
                        name=proposal.name,
                        proposal_id=proposal.id,
                        purpose=proposal.purpose,
                        revision=proposal.proposed_revision,
                        status=proposal.ledger_status,
                    )
                ).returning()
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["ledgers"]))
        return revision

    async def list_proposals(self) -> list[LedgerProposal[Fetched]]:
        """List pending proposals newest first for user inspection."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(LedgerProposal)
                .where(LedgerProposal.status.eq("pending"))
                .order_by(LedgerProposal.created_at.desc())
            )

    async def fetch_export(self, ledger_id: UUID) -> LedgerExportSnapshot:
        """Fetch definitions and complete entry history in one transaction."""
        async with self.database.transaction() as transaction:
            ledger = await transaction.fetch_one_or_none(
                select(Ledger).where(Ledger.id.eq(ledger_id))
            )
            if ledger is None:
                raise LedgerNotFoundError(str(ledger_id))
            revisions = await transaction.fetch_all(
                select(LedgerRevision)
                .where(LedgerRevision.ledger_id.eq(ledger_id))
                .order_by(LedgerRevision.revision.asc())
            )
            proposals = await transaction.fetch_all(
                select(LedgerProposal)
                .where(LedgerProposal.ledger_id.eq(ledger_id))
                .order_by(LedgerProposal.proposed_revision.asc())
            )
            entries = await transaction.fetch_all(
                select(LedgerEntry)
                .where(LedgerEntry.ledger_id.eq(ledger_id))
                .order_by(LedgerEntry.recorded_at.asc())
            )
        superseder_by_target = {
            entry.supersedes_entry_id: entry.id
            for entry in entries
            if entry.supersedes_entry_id is not None
        }
        return LedgerExportSnapshot(
            entries=[
                LedgerEntryView(
                    entry=entry,
                    superseded_by_entry_id=superseder_by_target.get(entry.id),
                )
                for entry in entries
            ],
            ledger_id=ledger.id,
            proposals=proposals,
            revisions=revisions,
        )

    async def fetch_revisions(
        self,
        ledger_id: UUID,
    ) -> list[LedgerRevision[Fetched]]:
        """Fetch every approved revision newest first."""
        async with self.database.transaction() as transaction:
            revisions = await transaction.fetch_all(
                select(LedgerRevision)
                .where(LedgerRevision.ledger_id.eq(ledger_id))
                .order_by(LedgerRevision.revision.desc())
            )
        if not revisions:
            raise LedgerNotFoundError(str(ledger_id))
        return revisions

    async def query_entries(
        self,
        query: LedgerEntryQuery,
    ) -> list[LedgerEntryView]:
        """Query current records or immutable history through bounded filters."""
        async with self.database.transaction() as transaction:
            if query.ledger_id is not None:
                ledger = await transaction.fetch_one_or_none(
                    select(Ledger).where(Ledger.id.eq(query.ledger_id))
                )
                if ledger is None:
                    raise LedgerNotFoundError(str(query.ledger_id))
                entries_query = select(LedgerEntry).where(
                    LedgerEntry.ledger_id.eq(query.ledger_id)
                )
            else:
                entries_query = select(LedgerEntry).all()
            entries = await transaction.fetch_all(
                entries_query.order_by(LedgerEntry.recorded_at.desc())
            )
        superseder_by_target = {
            entry.supersedes_entry_id: entry.id
            for entry in entries
            if entry.supersedes_entry_id is not None
        }
        return [
            LedgerEntryView(
                entry=entry,
                superseded_by_entry_id=superseder_by_target.get(entry.id),
            )
            for entry in entries
            if _entry_matches_query(entry, query, superseder_by_target)
        ][: query.limit]

    async def list_entries(
        self,
        ledger_id: UUID,
        *,
        include_superseded: bool = False,
    ) -> list[LedgerEntryView]:
        """List current entries or the complete immutable history newest first."""
        async with self.database.transaction() as transaction:
            ledger = await transaction.fetch_one_or_none(
                select(Ledger).where(Ledger.id.eq(ledger_id))
            )
            if ledger is None:
                raise LedgerNotFoundError(str(ledger_id))
            entries = await transaction.fetch_all(
                select(LedgerEntry)
                .where(LedgerEntry.ledger_id.eq(ledger_id))
                .order_by(LedgerEntry.recorded_at.desc())
            )
        superseder_by_target = {
            entry.supersedes_entry_id: entry.id
            for entry in entries
            if entry.supersedes_entry_id is not None
        }
        return [
            LedgerEntryView(
                entry=entry,
                superseded_by_entry_id=superseder_by_target.get(entry.id),
            )
            for entry in entries
            if include_superseded or entry.id not in superseder_by_target
        ]

    async def list_ledgers(self) -> list[LedgerRevision[Fetched]]:
        """List each Ledger at its latest approved revision."""
        async with self.database.transaction() as transaction:
            revisions = await transaction.fetch_all(
                select(LedgerRevision).all().order_by(LedgerRevision.revision.desc())
            )
        latest: dict[UUID7, LedgerRevision[Fetched]] = {}
        for revision in revisions:
            if revision.ledger_id not in latest:
                latest[revision.ledger_id] = revision
        return sorted(
            latest.values(),
            key=lambda revision: revision.created_at,
            reverse=True,
        )

    async def _fetch_append_revision(
        self,
        transaction: Transaction,
        ledger_id: UUID7,
        revision: PositiveInt,
    ) -> LedgerRevision[Fetched]:
        """Require the exact current active schema before validating a batch."""
        current = await transaction.fetch_one_or_none(
            select(LedgerRevision)
            .where(LedgerRevision.ledger_id.eq(ledger_id))
            .order_by(LedgerRevision.revision.desc())
            .limit(1)
        )
        if current is None:
            raise LedgerNotFoundError(str(ledger_id))
        if current.revision != revision:
            message = (
                f"Ledger {ledger_id} changed from revision {revision} "
                f"to {current.revision}"
            )
            raise LedgerConflictError(message)
        if current.status != "active":
            message = "Completed or abandoned Ledgers reject entries"
            raise LedgerConflictError(message)
        return current

    async def _plan_entries(
        self,
        transaction: Transaction,
        current: LedgerRevision[Fetched],
        drafts: list[LedgerEntryDraft],
        *,
        evidence: LedgerUserEvidence,
    ) -> list[_PlannedLedgerEntry]:
        """Validate every batch member and resolve retries before any insert."""
        planned: list[_PlannedLedgerEntry] = []
        for batch_index, draft in enumerate(drafts):
            dedupe_key = _entry_dedupe_key(
                batch_index=batch_index,
                draft=draft,
                evidence=evidence,
                ledger_id=current.ledger_id,
                revision=current.revision,
            )
            planned.append(
                _PlannedLedgerEntry(
                    dedupe_key=dedupe_key,
                    draft=draft,
                    existing=await transaction.fetch_one_or_none(
                        select(LedgerEntry).where(LedgerEntry.dedupe_key.eq(dedupe_key))
                    ),
                    values=_validated_entry_values(current, draft),
                )
            )
        return planned

    async def _validate_correction_targets(
        self,
        transaction: Transaction,
        ledger_id: UUID7,
        planned: list[_PlannedLedgerEntry],
    ) -> None:
        """Reject unknown, cross-Ledger, or branching correction targets."""
        correction_targets = [
            entry.draft.supersedes_entry_id
            for entry in planned
            if entry.existing is None and entry.draft.supersedes_entry_id is not None
        ]
        if len(set(correction_targets)) != len(correction_targets):
            message = "One batch cannot supersede the same Ledger entry twice"
            raise InvalidLedgerError(message)
        for target_id in correction_targets:
            target = await transaction.fetch_one_or_none(
                select(LedgerEntry).where(LedgerEntry.id.eq(target_id))
            )
            if target is None:
                message = "Superseded Ledger entry was not found"
                raise InvalidLedgerError(message)
            if target.ledger_id != ledger_id:
                message = "A correction cannot cross Ledger identity"
                raise InvalidLedgerError(message)
            superseder = await transaction.fetch_one_or_none(
                select(LedgerEntry).where(LedgerEntry.supersedes_entry_id.eq(target_id))
            )
            if superseder is not None:
                message = "Ledger entry is already superseded"
                raise LedgerConflictError(message)

    async def _append_planned_entries(
        self,
        transaction: Transaction,
        ledger_id: UUID7,
        revision: PositiveInt,
        planned: list[_PlannedLedgerEntry],
        *,
        evidence: LedgerUserEvidence,
    ) -> list[LedgerEntry[Fetched]]:
        """Insert validated members while collapsing retries within the batch."""
        entries: list[LedgerEntry[Fetched]] = []
        appended_by_key: dict[str, LedgerEntry[Fetched]] = {}
        for planned_entry in planned:
            repeated = planned_entry.existing or appended_by_key.get(
                planned_entry.dedupe_key
            )
            if repeated is not None:
                entries.append(repeated)
                continue
            appended = await transaction.execute(
                insert(
                    LedgerEntry(
                        dedupe_key=planned_entry.dedupe_key,
                        evidence=[f"tether://message/{evidence.message_id}"],
                        ledger_id=ledger_id,
                        occurred_at=planned_entry.draft.occurred_at,
                        revision=revision,
                        source_message_id=evidence.message_id,
                        supersedes_entry_id=(planned_entry.draft.supersedes_entry_id),
                        values=planned_entry.values,
                    )
                ).returning()
            )
            appended_by_key[planned_entry.dedupe_key] = appended
            entries.append(appended)
        return entries


__all__ = [
    "LedgerEntryView",
    "LedgerExportSnapshot",
    "LedgerService",
    "LedgerUserEvidence",
]
