"""Open WebUI tool descriptors, invocation, and bounded envelopes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request

from tether.bucket_item_store import BucketItemProvenance
from tether.capability_contracts import (
    CapabilityOutcome,
    ErrorRule,
    ToolErrorCode,
    catchable_exceptions,
    match_rule,
)


class ToolError(BaseModel):
    """Expected failure detail in a `success:false` tool envelope."""

    code: ToolErrorCode
    message: str


class ToolEnvelope(BaseModel):
    """Uniform success or expected-failure result returned by every tool."""

    success: bool
    result: Any = None
    error: ToolError | None = None
    provenance: BucketItemProvenance | None = None


def _failure(code: ToolErrorCode, message: str) -> ToolEnvelope:
    """Envelope an expected failure without partial state."""
    return ToolEnvelope(success=False, error=ToolError(code=code, message=message))


def _success(outcome: CapabilityOutcome) -> ToolEnvelope:
    """Envelope a successful capability result and optional metadata."""
    return ToolEnvelope(
        success=True,
        result=outcome.result,
        provenance=outcome.provenance,
    )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool's stable name, parameter model, handler, and declared errors."""

    name: str
    params_model: type[BaseModel]
    handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]]
    errors: tuple[ErrorRule, ...] = ()


async def invoke_tool_spec(
    spec: ToolSpec, request: Request, params: BaseModel
) -> ToolEnvelope:
    """Invoke a tool while translating only declared domain failures."""
    catchable = catchable_exceptions(spec.errors)
    try:
        outcome = await spec.handler(request, params)
    except catchable as error:
        rule = match_rule(spec.errors, error)
        message = "not found" if rule.code == "not_found" else str(error)
        return _failure(rule.code, message)
    return _success(outcome)


__all__ = [
    "ToolEnvelope",
    "ToolError",
    "ToolSpec",
    "invoke_tool_spec",
]
