"""The internal Proposal tool surface: `propose` and `list_proposals` only.

These mount alongside the other `/internal/tools/*` endpoints — the loopback
seam a pi process calls back into. Composing a proposal (`propose`) is the only
way a gated action set comes into being, and the agent can see what it has
queued (`list_proposals`). Approve, reject, grant, and revoke are deliberately
**absent** from this surface: they are human-only HTTP routes, so the closed
tool world (ADR 0005/0014) makes the gate impossible to talk an agent around.
"""

from __future__ import annotations

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether import proposal_capabilities
from tether.capabilities import CapabilityOutcome, bind_params
from tether.proposal_capabilities import PROPOSAL_ERRORS
from tether.proposals import ActionDraft, ProposalDraft, ProposalState
from tether.tools import ToolSpec


class ProposeActionParam(BaseModel):
    """One typed action to compose into a proposal.

    `kind` names a registered action kind; `scope` is an optional consumer-
    defined category string (unvalidated — a typo simply fails to match a
    grant); `params` are the kind's params, validated against its model.
    `display` is an optional human-readable one-line summary shown in the
    Proposals panel; when omitted the panel renders the kind and params.
    """

    kind: str
    scope: str | None = None
    params: dict[str, object] = {}
    display: str | None = None


class ProposeParams(BaseModel):
    """Params for composing a proposal — an explicit set of typed actions.

    The proposal executes immediately only if every action is covered by an
    existing autonomy grant; otherwise the whole set queues for human review.
    Approval, rejection, and granting are human-only and never tools.
    """

    consumer: str
    title: str
    summary: str
    actions: list[ProposeActionParam]


class ListProposalsParams(BaseModel):
    """Params for listing proposals, newest first, capped at `limit`."""

    state: ProposalState | None = None
    limit: PositiveInt = 50


async def _propose(request: Request, params: ProposeParams) -> CapabilityOutcome:
    """Project the flat tool params onto the shared propose capability."""
    draft = ProposalDraft(
        consumer=params.consumer,
        title=params.title,
        summary=params.summary,
        actions=[
            ActionDraft(
                kind=action.kind,
                scope=action.scope,
                params=action.params,
                display=action.display,
            )
            for action in params.actions
        ],
    )
    return await proposal_capabilities.propose(request, draft)


PROPOSAL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("propose", ProposeParams, _propose, PROPOSAL_ERRORS),
    ToolSpec(
        "list_proposals",
        ListProposalsParams,
        bind_params(proposal_capabilities.list_proposals),
        PROPOSAL_ERRORS,
    ),
)
"""The Proposal capabilities exposed as internal tools, in order."""


def internal_proposal_tool_routes() -> list[Route]:
    """Mount the two Proposal tools as `/internal/tools/*` POST endpoints."""
    return [spec.route() for spec in PROPOSAL_TOOL_SPECS]
