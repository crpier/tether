"""Human-only HTTP routes for Proposals and autonomy grants.

Approve, reject, grant, and revoke live here and *only* here — never on the tool
surface — so trust promotion stays a human act (ADR 0014). Approve and reject
are optimistic-concurrency checked: the client sends the `version` it last
observed and a version that has moved on surfaces as a 409. Grant revocation is
convergent (a missing or already-revoked id is a no-op 204).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import proposal_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint
from tether.proposal_capabilities import (
    PROPOSAL_ERRORS,
    GrantRead,
    GrantSuggestionRead,
    ProposalRead,
    RejectionRead,
)
from tether.proposals import ProposalNotFoundError, ProposalState


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


def _path_proposal_id(request: Request) -> UUID:
    """Parse the `{proposal_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["proposal_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise ProposalNotFoundError(raw_id) from error


def _path_grant_id(request: Request) -> UUID:
    """Parse the `{grant_id}` path segment; a bad id names nothing (no-op)."""
    raw_id = request.path_params["grant_id"]
    try:
        return UUID(raw_id)
    except ValueError:
        return UUID(int=0)


_translate_domain_errors = translate_domain_errors(PROPOSAL_ERRORS)


@endpoint(response=ProposalRead, response_is_list=True)
async def list_proposals(request: Request) -> Response:
    """List proposals newest first, optionally filtered by state."""
    query = ListProposalsQuery.model_validate(dict(request.query_params))
    return rest_response(
        await proposal_capabilities.list_proposals(request, state=query.state)
    )


@endpoint(response=ProposalRead)
@_translate_domain_errors
async def get_proposal(request: Request) -> Response:
    """Fetch one proposal bundled with its actions."""
    return rest_response(
        await proposal_capabilities.get(request, _path_proposal_id(request))
    )


@endpoint(request_body=ApproveProposalRequest, response=ProposalRead)
@_translate_domain_errors
async def approve_proposal(request: Request, body: ApproveProposalRequest) -> Response:
    """Approve a pending proposal, then execute its approved actions."""
    outcome = await proposal_capabilities.approve(
        request,
        _path_proposal_id(request),
        body.version,
        set(body.deselected_action_ids),
    )
    return rest_response(outcome)


@endpoint(request_body=RejectProposalRequest, response=RejectionRead)
@_translate_domain_errors
async def reject_proposal(request: Request, body: RejectProposalRequest) -> Response:
    """Reject a pending proposal (terminal), returning any revocable grants."""
    outcome = await proposal_capabilities.reject(
        request, _path_proposal_id(request), body.version, body.reason
    )
    return rest_response(outcome)


@endpoint(request_body=CreateGrantRequest, response=GrantRead, status=201)
async def create_grant(request: Request, body: CreateGrantRequest) -> Response:
    """Grant autonomy for a `(kind, scope)` category."""
    outcome = await proposal_capabilities.grant(request, body.kind, body.scope)
    return rest_response(outcome, status_code=201)


@endpoint(response=GrantRead, response_is_list=True)
async def list_grants(request: Request) -> Response:
    """List live (unrevoked) grants, newest first."""
    return rest_response(await proposal_capabilities.list_grants(request))


@endpoint(response=GrantSuggestionRead, response_is_list=True)
async def grant_suggestions(request: Request) -> Response:
    """Read-time grant suggestions for ungranted categories with history."""
    return rest_response(await proposal_capabilities.suggestions(request))


@endpoint(status=204)
async def revoke_grant(request: Request) -> Response:
    """Revoke a grant convergently; an absent/already-revoked id is a no-op."""
    _ = await proposal_capabilities.revoke(request, _path_grant_id(request))
    return Response(status_code=204)


proposal_routes: list[Route] = [
    EndpointRoute("/api/proposals", list_proposals, methods=["GET"]),
    EndpointRoute("/api/proposals/{proposal_id}", get_proposal, methods=["GET"]),
    EndpointRoute(
        "/api/proposals/{proposal_id}/approve", approve_proposal, methods=["POST"]
    ),
    EndpointRoute(
        "/api/proposals/{proposal_id}/reject", reject_proposal, methods=["POST"]
    ),
    EndpointRoute("/api/grants", create_grant, methods=["POST"]),
    EndpointRoute("/api/grants", list_grants, methods=["GET"]),
    EndpointRoute("/api/grants/suggestions", grant_suggestions, methods=["GET"]),
    EndpointRoute("/api/grants/{grant_id}", revoke_grant, methods=["DELETE"]),
]
