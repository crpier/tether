"""The Proposal domain's capability descriptor.

The pieces the human-only REST routes (`tether.proposal_routes`) and the two
agent tools (`tether.proposal_tools`) share live here once: the Read models,
the detached-reference builder, the domain->code map (`PROPOSAL_ERRORS`), and
one execute function per capability. Only `propose` and `list_proposals` are
reachable as tools; approve/reject/grant/revoke are route-only, keeping the gate
outside the closed tool world (ADR 0014).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.proposals import (
    ActionDisposition,
    ActionOutcome,
    AutonomyGrant,
    Fetched,
    GrantSuggestion,
    InvalidActionError,
    Proposal,
    ProposalAction,
    ProposalConflictError,
    ProposalDraft,
    ProposalNotFoundError,
    ProposalState,
    ProposalStateError,
    ProposalView,
    RejectionOutcome,
)

PROPOSAL_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((ProposalNotFoundError,), "not_found", 404, detail="proposal not found"),
    ErrorRule((ProposalConflictError,), "conflict", 409),
    ErrorRule((ProposalStateError,), "conflict", 409),
    ErrorRule((InvalidActionError,), "invalid_input", 422),
)
"""The proposal domain->code map both surfaces translate failures through."""


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a stored timestamp as UTC-aware; SQLite writes naive timestamps."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ProposalActionRead(BaseModel):
    """HTTP representation of one action within a proposal."""

    id: UUID
    seq: int
    kind: str
    scope: str | None
    params: dict[str, object]
    disposition: ActionDisposition
    outcome: ActionOutcome | None
    outcome_detail: str | None
    executed_at: datetime | None

    @classmethod
    def from_action(cls, action: ProposalAction[Fetched]) -> ProposalActionRead:
        """Render a stored action, parsing its params JSON back to an object."""
        return cls(
            id=action.id,
            seq=action.seq,
            kind=action.kind,
            scope=action.scope,
            params=cast("dict[str, object]", json.loads(action.params_json)),
            disposition=action.disposition,
            outcome=action.outcome,
            outcome_detail=action.outcome_detail,
            executed_at=_as_utc(action.executed_at),
        )


class ProposalRead(BaseModel):
    """HTTP representation of a proposal bundled with its actions."""

    id: UUID
    consumer: str
    title: str
    summary: str
    producing_run_id: str | None
    state: ProposalState
    rejection_reason: str | None
    version: PositiveInt
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    actions: list[ProposalActionRead]

    @classmethod
    def from_view(cls, view: ProposalView) -> ProposalRead:
        """Render a proposal view (proposal + actions) for clients."""
        proposal = view.proposal
        created = _as_utc(proposal.created_at)
        updated = _as_utc(proposal.updated_at)
        assert created is not None
        assert updated is not None
        return cls(
            id=proposal.id,
            consumer=proposal.consumer,
            title=proposal.title,
            summary=proposal.summary,
            producing_run_id=proposal.producing_run_id,
            state=proposal.state,
            rejection_reason=proposal.rejection_reason,
            version=proposal.version,
            created_at=created,
            updated_at=updated,
            decided_at=_as_utc(proposal.decided_at),
            actions=[ProposalActionRead.from_action(a) for a in view.actions],
        )


class ProposalCreationRead(BaseModel):
    """The result of composing a proposal: the proposal plus its disposition."""

    proposal: ProposalRead
    auto_executed: bool


class RejectionRead(BaseModel):
    """A rejected proposal plus the grants a human may now want to revoke."""

    proposal: ProposalRead
    revocable_grant_ids: list[UUID]


class GrantRead(BaseModel):
    """HTTP representation of a live autonomy grant."""

    id: UUID
    kind: str
    scope: str | None
    granted_at: datetime

    @classmethod
    def from_grant(cls, grant: AutonomyGrant[Fetched]) -> GrantRead:
        """Render a stored grant for clients."""
        granted = _as_utc(grant.granted_at)
        assert granted is not None
        return cls(id=grant.id, kind=grant.kind, scope=grant.scope, granted_at=granted)


class GrantSuggestionRead(BaseModel):
    """HTTP representation of a read-time calibration suggestion."""

    kind: str
    scope: str | None
    seen: int
    approved: int
    rejected: int
    edited: int
    last_rejection: datetime | None

    @classmethod
    def from_suggestion(cls, suggestion: GrantSuggestion) -> GrantSuggestionRead:
        """Render a calibration suggestion for clients."""
        return cls(
            kind=suggestion.kind,
            scope=suggestion.scope,
            seen=suggestion.seen,
            approved=suggestion.approved,
            rejected=suggestion.rejected,
            edited=suggestion.edited,
            last_rejection=_as_utc(suggestion.last_rejection),
        )


def _proposal_reference(proposal_id: UUID, version: PositiveInt) -> Proposal[Fetched]:
    """Build a detached proposal carrying only the identity a mutation acts on.

    Approve/Reject read just `id` and `version` for their optimistic-concurrency
    check and re-fetch the live row; the other columns are placeholders.
    """
    return cast(
        "Proposal[Fetched]",
        Proposal.construct(
            id=proposal_id,
            version=version,
            consumer="",
            title="",
            summary="",
            state="pending",
        ),
    )


def _single(view: ProposalView) -> CapabilityOutcome:
    """Render a single-proposal outcome."""
    return CapabilityOutcome(
        result=ProposalRead.from_view(view).model_dump(mode="json")
    )


async def propose(request: Request, draft: ProposalDraft) -> CapabilityOutcome:
    """Compose a proposal; it auto-executes iff every action is grant-covered."""
    creation = await request.app.state.proposal_service.create(
        draft,
        producing_run_id=getattr(request.state, "session_id", None),
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=ProposalCreationRead(
            proposal=ProposalRead.from_view(creation.proposal),
            auto_executed=creation.auto_executed,
        ).model_dump(mode="json")
    )


async def list_proposals(
    request: Request, state: ProposalState | None = None, limit: int | None = None
) -> CapabilityOutcome:
    """List proposals newest first, each bundled with its actions."""
    views = await request.app.state.proposal_service.list_proposals(
        state=state, limit=limit, logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result=[ProposalRead.from_view(view).model_dump(mode="json") for view in views]
    )


async def get(request: Request, proposal_id: UUID) -> CapabilityOutcome:
    """Fetch one proposal bundled with its actions."""
    view = await request.app.state.proposal_service.get(proposal_id)
    return _single(view)


async def approve(
    request: Request,
    proposal_id: UUID,
    version: PositiveInt,
    deselected_action_ids: set[UUID7],
) -> CapabilityOutcome:
    """Approve a pending proposal at an observed version, then execute it."""
    view = await request.app.state.proposal_service.approve(
        _proposal_reference(proposal_id, version),
        deselected_action_ids=deselected_action_ids,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return _single(view)


async def reject(
    request: Request, proposal_id: UUID, version: PositiveInt, reason: str | None
) -> CapabilityOutcome:
    """Reject a pending proposal (terminal), returning any revocable grants."""
    outcome: RejectionOutcome = await request.app.state.proposal_service.reject(
        _proposal_reference(proposal_id, version),
        reason=reason,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=RejectionRead(
            proposal=ProposalRead.from_view(outcome.proposal),
            revocable_grant_ids=[UUID(str(g)) for g in outcome.revocable_grant_ids],
        ).model_dump(mode="json")
    )


async def grant(
    request: Request, kind: str, scope: str | None = None
) -> CapabilityOutcome:
    """Grant autonomy for a `(kind, scope)` category (a new ledger row)."""
    granted = await request.app.state.proposal_service.grant(
        kind, scope, now=datetime.now(UTC)
    )
    return CapabilityOutcome(
        result=GrantRead.from_grant(granted).model_dump(mode="json")
    )


async def revoke(request: Request, grant_id: UUID) -> CapabilityOutcome:
    """Revoke a grant convergently; an absent/already-revoked id is a no-op."""
    await request.app.state.proposal_service.revoke(grant_id, now=datetime.now(UTC))
    return CapabilityOutcome(result=None)


async def list_grants(request: Request) -> CapabilityOutcome:
    """List live (unrevoked) grants, newest first."""
    grants = await request.app.state.proposal_service.list_grants()
    return CapabilityOutcome(
        result=[GrantRead.from_grant(g).model_dump(mode="json") for g in grants]
    )


async def suggestions(request: Request) -> CapabilityOutcome:
    """Read-time grant suggestions for ungranted categories with history."""
    computed = await request.app.state.proposal_service.calibration_stats()
    return CapabilityOutcome(
        result=[
            GrantSuggestionRead.from_suggestion(s).model_dump(mode="json")
            for s in computed
        ]
    )
