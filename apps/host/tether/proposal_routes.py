"""Human-only HTTP routes for Proposals and autonomy grants.

Approve, reject, grant, and revoke live here and *only* here — never on the tool
surface — so trust promotion stays a human act (ADR 0014). Approve and reject
are optimistic-concurrency checked: the client sends the `version` it last
observed and a version that has moved on surfaces as a 409. Grant revocation is
convergent (a missing or already-revoked id is a no-op 204).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response

from tether import proposal_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.proposal_capabilities import (
    PROPOSAL_ERRORS,
    GrantRead,
    GrantSuggestionRead,
    ProposalCountsRead,
    ProposalRead,
    RejectionRead,
)
from tether.proposal_errors import ProposalNotFoundError
from tether.proposal_store import ProposalState


class ApproveProposalRequest(BaseModel):
    """Body for approving a proposal at an observed version.

    `deselected_action_ids` unticks individual actions before approval; the
    rest are approved and executed by the host.
    """

    version: PositiveInt
    deselected_action_ids: list[UUID7] = []


class RejectProposalRequest(BaseModel):
    """Body for rejecting a proposal at an observed version."""

    version: PositiveInt
    reason: str | None = None


class ListProposalsQuery(BaseModel):
    """Query string filtering the proposal list by lifecycle state."""

    state: ProposalState | None = None


class CreateGrantRequest(BaseModel):
    """Body for granting autonomy over a `(kind, scope)` category."""

    kind: str
    scope: str | None = None


def _path_proposal_id(raw_id: str) -> UUID:
    """Parse the `{proposal_id}` path segment, treating a bad id as absent."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise ProposalNotFoundError(raw_id) from error


def _path_grant_id(raw_id: str) -> UUID:
    """Parse the `{grant_id}` path segment; a bad id names nothing (no-op)."""
    try:
        return UUID(raw_id)
    except ValueError:
        return UUID(int=0)


_translate_domain_errors = translate_domain_errors(PROPOSAL_ERRORS)


router = APIRouter()


@router.get("/api/proposals", response_model=list[ProposalRead])
async def list_proposals(request: Request) -> Response:
    """List proposals newest first, optionally filtered by state."""
    query = ListProposalsQuery.model_validate(dict(request.query_params))
    return rest_response(
        await proposal_capabilities.list_proposals(request, state=query.state)
    )


@router.get("/api/proposals/counts", response_model=ProposalCountsRead)
async def proposal_counts(request: Request) -> Response:
    """Count proposals for the queue and history tab labels."""
    return rest_response(await proposal_capabilities.counts(request))


@router.get("/api/proposals/{proposal_id}", response_model=ProposalRead)
@_translate_domain_errors
async def get_proposal(request: Request, proposal_id: str) -> Response:
    """Fetch one proposal bundled with its actions."""
    return rest_response(
        await proposal_capabilities.get(request, _path_proposal_id(proposal_id))
    )


@router.post("/api/proposals/{proposal_id}/approve", response_model=ProposalRead)
@_translate_domain_errors
async def approve_proposal(
    request: Request, body: ApproveProposalRequest, proposal_id: str
) -> Response:
    """Approve a pending proposal, then execute its approved actions."""
    outcome = await proposal_capabilities.approve(
        request,
        _path_proposal_id(proposal_id),
        body.version,
        set(body.deselected_action_ids),
    )
    return rest_response(outcome)


@router.post("/api/proposals/{proposal_id}/reject", response_model=RejectionRead)
@_translate_domain_errors
async def reject_proposal(
    request: Request, body: RejectProposalRequest, proposal_id: str
) -> Response:
    """Reject a pending proposal (terminal), returning any revocable grants."""
    outcome = await proposal_capabilities.reject(
        request, _path_proposal_id(proposal_id), body.version, body.reason
    )
    return rest_response(outcome)


@router.post("/api/grants", response_model=GrantRead, status_code=201)
async def create_grant(request: Request, body: CreateGrantRequest) -> Response:
    """Grant autonomy for a `(kind, scope)` category."""
    outcome = await proposal_capabilities.grant(request, body.kind, body.scope)
    return rest_response(outcome, status_code=201)


@router.get("/api/grants", response_model=list[GrantRead])
async def list_grants(request: Request) -> Response:
    """List live (unrevoked) grants, newest first."""
    return rest_response(await proposal_capabilities.list_grants(request))


@router.get("/api/grants/suggestions", response_model=list[GrantSuggestionRead])
async def grant_suggestions(request: Request) -> Response:
    """Read-time grant suggestions for ungranted categories with history."""
    return rest_response(await proposal_capabilities.suggestions(request))


@router.delete("/api/grants/{grant_id}", status_code=204)
async def revoke_grant(request: Request, grant_id: str) -> Response:
    """Revoke a grant convergently; an absent/already-revoked id is a no-op."""
    _ = await proposal_capabilities.revoke(request, _path_grant_id(grant_id))
    return Response(status_code=204)
