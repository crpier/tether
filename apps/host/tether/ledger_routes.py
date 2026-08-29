"""Authenticated browser routes for inspecting generic Ledgers."""

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import Response

from tether import ledger_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.ledger_capabilities import (
    LEDGER_ERRORS,
    LedgerDetailRead,
    LedgerEntryRead,
    LedgerExportRead,
    LedgerProposalRead,
    LedgerRead,
)

router = APIRouter()
_translate_domain_errors = translate_domain_errors(LEDGER_ERRORS)


@router.get("/api/ledgers", response_model=list[LedgerRead])
async def list_ledgers(request: Request) -> Response:
    """List current approved Ledger definitions."""
    return rest_response(await ledger_capabilities.list_ledgers(request))


@router.get(
    "/api/ledgers/{ledger_id}/export",
    response_model=LedgerExportRead,
)
@_translate_domain_errors
async def export_ledger(request: Request, ledger_id: UUID) -> Response:
    """Export one complete deterministic Ledger history."""
    return rest_response(await ledger_capabilities.export_ledger(request, ledger_id))


@router.get(
    "/api/ledgers/{ledger_id}/entries",
    response_model=list[LedgerEntryRead],
)
@_translate_domain_errors
async def list_ledger_entries(
    request: Request,
    ledger_id: UUID,
    *,
    include_superseded: bool = False,
) -> Response:
    """List current records or complete immutable history newest first."""
    return rest_response(
        await ledger_capabilities.list_entries(
            request,
            ledger_id,
            include_superseded=include_superseded,
        )
    )


@router.get("/api/ledgers/{ledger_id}", response_model=LedgerDetailRead)
@_translate_domain_errors
async def fetch_ledger(request: Request, ledger_id: UUID) -> Response:
    """Return current Ledger state and immutable definition history."""
    return rest_response(await ledger_capabilities.fetch_ledger(request, ledger_id))


@router.get(
    "/api/ledger-proposals",
    response_model=list[LedgerProposalRead],
)
async def list_ledger_proposals(request: Request) -> Response:
    """List pending exact definitions awaiting user approval."""
    return rest_response(await ledger_capabilities.list_proposals(request))
