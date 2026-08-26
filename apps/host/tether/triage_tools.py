"""The Open WebUI deterministic Bucket triage tool descriptor."""

from __future__ import annotations

from pydantic import BaseModel
from starlette.requests import Request

from tether.app_runtime import app_runtime
from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome
from tether.structured_logging import get_request_logger
from tether.tool_runtime import ToolSpec


class TriageReportParams(BaseModel):
    """Params for a bounded report over the active Bucket list."""


async def _triage_report(request: Request) -> CapabilityOutcome:
    """Compute the read-only Triage report over the live active Bucket list."""
    report = await app_runtime(request.app).triage_service.triage_report(
        logger=get_request_logger(request)
    )
    return CapabilityOutcome(result=report.model_dump(mode="json"))


TRIAGE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("triage_report", TriageReportParams, bind_params(_triage_report)),
)
"""The Triage report exposed to Open WebUI."""
