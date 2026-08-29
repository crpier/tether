"""Foreground tools for generic Ledger definitions and records."""

from __future__ import annotations

from pydantic import UUID7, BaseModel, Field, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome
from tether.ledger_capabilities import (
    LEDGER_ERRORS,
    append_entries,
    approve_proposal,
    list_ledgers,
    list_proposals,
    propose,
    propose_revision,
    query_entries,
)
from tether.ledger_model import (
    LEDGER_ENTRY_BATCH_LIMIT,
    LedgerDefinition,
    LedgerEntryDraft,
    LedgerEntryQuery,
)
from tether.tool_runtime import ToolSpec


class ProposeLedgerParams(LedgerDefinition):
    """Propose a flat structured history for later explicit user approval.

    Use only when no established Vertical owns the records. This freezes a
    proposal but creates no writable Ledger. A later interactive user Message
    must approve the exact proposal.
    """


class AppendLedgerEntriesParams(BaseModel):
    """Append an atomic bounded batch under one observed active schema.

    Values must satisfy the approved field definitions. Every appended record
    cites the active user Message automatically. Set `supersedes_entry_id` only
    for a complete correction of one current entry.
    """

    entries: list[LedgerEntryDraft] = Field(
        min_length=1,
        max_length=LEDGER_ENTRY_BATCH_LIMIT,
    )
    ledger_id: UUID7
    revision: PositiveInt


class QueryLedgerEntriesParams(LedgerEntryQuery):
    """Query bounded generic records separately from current Memory Search."""


class ProposeLedgerRevisionParams(LedgerDefinition):
    """Propose an exact successor to one observed active Ledger revision."""

    ledger_id: UUID7
    revision: PositiveInt


class ApproveLedgerProposalParams(BaseModel):
    """Approve one exact proposal after the user explicitly confirms it."""

    proposal_id: UUID7


class ListLedgersParams(BaseModel):
    """List current approved generic Ledgers and their field schemas."""


class ListLedgerProposalsParams(BaseModel):
    """List pending Ledger definitions awaiting explicit user approval."""


async def _query_ledger_entries(
    request: Request,
    params: QueryLedgerEntriesParams,
) -> CapabilityOutcome:
    """Adapt the generated query model to one domain query value."""
    return await query_entries(request, LedgerEntryQuery.model_validate(params))


async def _propose_ledger_revision(
    request: Request,
    params: ProposeLedgerRevisionParams,
) -> CapabilityOutcome:
    """Separate revision identity from its complete proposed definition."""
    return await propose_revision(
        request,
        params.ledger_id,
        params.revision,
        LedgerDefinition.model_validate(
            params.model_dump(exclude={"ledger_id", "revision"})
        ),
    )


async def _propose_ledger(
    request: Request,
    params: ProposeLedgerParams,
) -> CapabilityOutcome:
    """Adapt the generated tool model to the immutable domain definition."""
    return await propose(request, LedgerDefinition.model_validate(params))


LEDGER_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "propose_ledger",
        ProposeLedgerParams,
        _propose_ledger,
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "append_ledger_entries",
        AppendLedgerEntriesParams,
        bind_params(append_entries),
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "query_ledger_entries",
        QueryLedgerEntriesParams,
        _query_ledger_entries,
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "propose_ledger_revision",
        ProposeLedgerRevisionParams,
        _propose_ledger_revision,
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "approve_ledger_proposal",
        ApproveLedgerProposalParams,
        bind_params(approve_proposal),
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "list_ledgers",
        ListLedgersParams,
        bind_params(list_ledgers),
        LEDGER_ERRORS,
    ),
    ToolSpec(
        "list_ledger_proposals",
        ListLedgerProposalsParams,
        bind_params(list_proposals),
        LEDGER_ERRORS,
    ),
)
"""Generic Ledger capabilities exposed to foreground chat."""


def internal_ledger_tool_routes() -> list[Route]:
    """Mount Ledger tools under `/internal/tools/*`."""
    return [spec.route() for spec in LEDGER_TOOL_SPECS]


__all__ = [
    "LEDGER_TOOL_SPECS",
    "AppendLedgerEntriesParams",
    "ApproveLedgerProposalParams",
    "ListLedgerProposalsParams",
    "ListLedgersParams",
    "ProposeLedgerParams",
    "ProposeLedgerRevisionParams",
    "QueryLedgerEntriesParams",
    "internal_ledger_tool_routes",
]
