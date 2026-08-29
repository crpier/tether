"""Shared chat-tool and HTTP capabilities for generic Ledgers."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.active_user_evidence import (
    ActiveUserEvidenceError,
    resolve_active_user_evidence,
)
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.conversations import ConversationService
from tether.ledger_errors import (
    InvalidLedgerError,
    LedgerConflictError,
    LedgerNotFoundError,
)
from tether.ledger_model import (
    LedgerDefinition,
    LedgerEntryDraft,
    LedgerEntryQuery,
    LedgerFieldDefinition,
    LedgerLifecycleStatus,
    LedgerProposalKind,
    LedgerProposalStatus,
    LedgerScalarValue,
)
from tether.ledger_store import LedgerEntry, LedgerProposal, LedgerRevision
from tether.ledgers import LedgerService, LedgerUserEvidence

LEDGER_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((LedgerNotFoundError,), "not_found", 404, detail="Ledger not found"),
    ErrorRule((LedgerConflictError,), "conflict", 409),
    ErrorRule((InvalidLedgerError,), "invalid_input", 422),
)
"""Ledger failures translated at both request seams."""


class LedgerEntryRead(BaseModel):
    """One immutable Ledger record with its exact interpretation and Evidence."""

    evidence: list[str]
    id: UUID7
    is_current: bool
    ledger_id: UUID7
    occurred_at: datetime | None
    recorded_at: datetime
    revision: PositiveInt
    superseded_by_entry_id: UUID7 | None
    supersedes_entry_id: UUID7 | None
    values: dict[str, LedgerScalarValue]

    @classmethod
    def from_entry(
        cls,
        entry: LedgerEntry[Fetched],
        *,
        superseded_by_entry_id: UUID7 | None = None,
    ) -> LedgerEntryRead:
        """Render one stored entry with projected supersession state."""
        return cls(
            evidence=entry.evidence,
            id=entry.id,
            is_current=superseded_by_entry_id is None,
            ledger_id=entry.ledger_id,
            occurred_at=entry.occurred_at,
            recorded_at=entry.recorded_at,
            revision=entry.revision,
            superseded_by_entry_id=superseded_by_entry_id,
            supersedes_entry_id=entry.supersedes_entry_id,
            values=entry.values,
        )


class LedgerRead(BaseModel):
    """One Ledger at a specific approved immutable revision."""

    approved_by_conversation_id: UUID7
    approved_by_message_id: UUID7
    created_at: datetime
    fields: list[LedgerFieldDefinition]
    id: UUID7
    name: str
    proposal_id: UUID7
    purpose: str
    revision: PositiveInt
    status: LedgerLifecycleStatus

    @classmethod
    def from_revision(cls, revision: LedgerRevision[Fetched]) -> LedgerRead:
        """Render stable Ledger identity from one stored revision row."""
        return cls(
            approved_by_conversation_id=revision.approved_by_conversation_id,
            approved_by_message_id=revision.approved_by_message_id,
            created_at=revision.created_at,
            fields=revision.fields,
            id=revision.ledger_id,
            name=revision.name,
            proposal_id=revision.proposal_id,
            purpose=revision.purpose,
            revision=revision.revision,
            status=revision.status,
        )


class LedgerDetailRead(BaseModel):
    """Current Ledger definition together with immutable revision history."""

    current: LedgerRead
    revisions: list[LedgerRead]


class LedgerProposalRead(BaseModel):
    """One exact Ledger definition awaiting or retaining approval."""

    approved_at: datetime | None
    approved_by_message_id: UUID7 | None
    base_revision: PositiveInt | None
    created_at: datetime
    fields: list[LedgerFieldDefinition]
    id: UUID7
    kind: LedgerProposalKind
    ledger_id: UUID7
    ledger_status: LedgerLifecycleStatus
    name: str
    proposed_by_conversation_id: UUID7
    proposed_by_message_id: UUID7
    proposed_revision: PositiveInt
    purpose: str
    status: LedgerProposalStatus

    @classmethod
    def from_proposal(
        cls,
        proposal: LedgerProposal[Fetched],
    ) -> LedgerProposalRead:
        """Render one stored proposal without exposing persistence details."""
        return cls.model_validate(proposal, from_attributes=True)


class LedgerExportRead(BaseModel):
    """Complete deterministic Ledger definition and record history."""

    entries: list[LedgerEntryRead]
    ledger_id: UUID7
    proposals: list[LedgerProposalRead]
    revisions: list[LedgerRead]


class _LedgerRuntime(Protocol):
    """Runtime dependencies required by Ledger capabilities."""

    conversation_service: ConversationService
    ledger_service: LedgerService
    trace_recorder: AgentTraceRecorder


def _runtime(request: Request) -> _LedgerRuntime:
    """Read Ledger dependencies from the canonical application runtime."""
    return cast("_LedgerRuntime", request.app.state.runtime)


async def _active_user_evidence(request: Request) -> LedgerUserEvidence:
    """Resolve exact fresh user authority without trusting copied tool text."""
    runtime = _runtime(request)
    try:
        source = await resolve_active_user_evidence(
            conversation_service=runtime.conversation_service,
            trace_recorder=runtime.trace_recorder,
            session_id=request.state.session_id,
        )
    except ActiveUserEvidenceError as error:
        message = "Ledger changes require active interactive user Evidence"
        raise InvalidLedgerError(message) from error
    return LedgerUserEvidence(
        conversation_id=source.conversation_id,
        message_id=source.id,
    )


def _proposal_outcome(proposal: LedgerProposal[Fetched]) -> CapabilityOutcome:
    """Render one proposal through the shared capability envelope."""
    return CapabilityOutcome(
        result=LedgerProposalRead.from_proposal(proposal).model_dump(mode="json")
    )


async def append_entries(
    request: Request,
    ledger_id: UUID7,
    revision: PositiveInt,
    entries: list[LedgerEntryDraft],
) -> CapabilityOutcome:
    """Append one atomic batch from fresh active user Evidence."""
    appended = await _runtime(request).ledger_service.append_entries(
        ledger_id,
        revision,
        entries,
        evidence=await _active_user_evidence(request),
    )
    return CapabilityOutcome(
        result=[
            LedgerEntryRead.from_entry(entry).model_dump(mode="json")
            for entry in appended
        ]
    )


async def query_entries(
    request: Request,
    query: LedgerEntryQuery,
) -> CapabilityOutcome:
    """Query bounded Ledger records without reading or mutating Memory."""
    views = await _runtime(request).ledger_service.query_entries(query)
    return CapabilityOutcome(
        result=[
            LedgerEntryRead.from_entry(
                view.entry,
                superseded_by_entry_id=view.superseded_by_entry_id,
            ).model_dump(mode="json")
            for view in views
        ]
    )


async def propose(
    request: Request,
    definition: LedgerDefinition,
) -> CapabilityOutcome:
    """Freeze one candidate Ledger definition from an active user turn."""
    return _proposal_outcome(
        await _runtime(request).ledger_service.propose(
            definition,
            evidence=await _active_user_evidence(request),
        )
    )


async def propose_revision(
    request: Request,
    ledger_id: UUID7,
    revision: PositiveInt,
    definition: LedgerDefinition,
) -> CapabilityOutcome:
    """Freeze one complete successor to an observed current revision."""
    return _proposal_outcome(
        await _runtime(request).ledger_service.propose_revision(
            ledger_id,
            revision,
            definition,
            evidence=await _active_user_evidence(request),
        )
    )


async def approve_proposal(
    request: Request,
    proposal_id: UUID7,
) -> CapabilityOutcome:
    """Approve one frozen proposal from a later active user Message."""
    revision = await _runtime(request).ledger_service.approve_proposal(
        proposal_id,
        evidence=await _active_user_evidence(request),
    )
    return CapabilityOutcome(
        result=LedgerRead.from_revision(revision).model_dump(mode="json")
    )


async def export_ledger(
    request: Request,
    ledger_id: UUID,
) -> CapabilityOutcome:
    """Export one transactionally consistent complete Ledger history."""
    snapshot = await _runtime(request).ledger_service.fetch_export(ledger_id)
    return CapabilityOutcome(
        result=LedgerExportRead(
            entries=[
                LedgerEntryRead.from_entry(
                    view.entry,
                    superseded_by_entry_id=view.superseded_by_entry_id,
                )
                for view in snapshot.entries
            ],
            ledger_id=snapshot.ledger_id,
            proposals=[
                LedgerProposalRead.from_proposal(proposal)
                for proposal in snapshot.proposals
            ],
            revisions=[
                LedgerRead.from_revision(revision) for revision in snapshot.revisions
            ],
        ).model_dump(mode="json")
    )


async def fetch_ledger(
    request: Request,
    ledger_id: UUID,
) -> CapabilityOutcome:
    """Fetch one Ledger and every approved definition revision."""
    revisions = await _runtime(request).ledger_service.fetch_revisions(ledger_id)
    rendered = [LedgerRead.from_revision(revision) for revision in revisions]
    return CapabilityOutcome(
        result=LedgerDetailRead(
            current=rendered[0],
            revisions=rendered,
        ).model_dump(mode="json")
    )


async def list_entries(
    request: Request,
    ledger_id: UUID,
    *,
    include_superseded: bool = False,
) -> CapabilityOutcome:
    """List current records or one Ledger's complete immutable history."""
    views = await _runtime(request).ledger_service.list_entries(
        ledger_id,
        include_superseded=include_superseded,
    )
    return CapabilityOutcome(
        result=[
            LedgerEntryRead.from_entry(
                view.entry,
                superseded_by_entry_id=view.superseded_by_entry_id,
            ).model_dump(mode="json")
            for view in views
        ]
    )


async def list_ledgers(request: Request) -> CapabilityOutcome:
    """List current approved Ledger definitions."""
    revisions = await _runtime(request).ledger_service.list_ledgers()
    return CapabilityOutcome(
        result=[
            LedgerRead.from_revision(revision).model_dump(mode="json")
            for revision in revisions
        ]
    )


async def list_proposals(request: Request) -> CapabilityOutcome:
    """List pending Ledger proposals for inspection or later approval."""
    proposals = await _runtime(request).ledger_service.list_proposals()
    return CapabilityOutcome(
        result=[
            LedgerProposalRead.from_proposal(proposal).model_dump(mode="json")
            for proposal in proposals
        ]
    )


__all__ = [
    "LEDGER_ERRORS",
    "LedgerDetailRead",
    "LedgerEntryRead",
    "LedgerExportRead",
    "LedgerProposalRead",
    "LedgerRead",
    "append_entries",
    "approve_proposal",
    "export_ledger",
    "fetch_ledger",
    "list_entries",
    "list_ledgers",
    "list_proposals",
    "propose",
    "propose_revision",
    "query_entries",
]
