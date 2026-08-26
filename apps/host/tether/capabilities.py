"""Parameter binding for retained Open WebUI capabilities."""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request

from tether.capability_contracts import CapabilityOutcome


def bind_params(
    execute: Callable[..., Awaitable[CapabilityOutcome]],
) -> Callable[[Request, BaseModel], Awaitable[CapabilityOutcome]]:
    """Bind fields from a validated parameter model onto a capability call."""

    async def bound(request: Request, params: BaseModel) -> CapabilityOutcome:
        fields: dict[str, Any] = {
            name: getattr(params, name) for name in type(params).model_fields
        }
        return await execute(request, **fields)

    return bound


__all__ = ["bind_params"]
