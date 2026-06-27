"""The internal Triage tool surface, over the shared response envelope.

`triage_report` mounts alongside the Bucket item tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and domain-error translation
(`tether.tools`). It is the agent's entry point for the Triage report, callable
on demand or from an agent-prompt Scheduled trigger (#18); either way it runs the
same host computation and stores nothing.
"""

from __future__ import annotations

from pydantic import BaseModel
from starlette.requests import Request
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.tools import ToolEndpoint, ToolEnvelope, ToolRoute


class TriageReportParams(BaseModel):
    """Params for the Triage report.

    The report is computed over the whole active Bucket list, so it takes no
    inputs beyond the session identity the gate already requires.
    """


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


async def _triage_report(request: Request, _params: TriageReportParams) -> ToolEnvelope:
    """Compute the read-only Triage report over the live active Bucket list."""
    report = await request.app.state.triage_service.triage_report(
        logger=_tool_logger(request)
    )
    return ToolEnvelope(success=True, result=report.model_dump(mode="json"))


def internal_triage_tool_routes() -> list[Route]:
    """Mount the Triage report as an `/internal/tools/*` POST endpoint.

    Returned separately from the public routes (and the other tools) so it stays
    absent from the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/triage_report",
            ToolEndpoint(TriageReportParams, _triage_report),
            methods=["POST"],
        ),
    ]
