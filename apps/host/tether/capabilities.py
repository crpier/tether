"""REST and parameter-binding adapters for shared capability contracts."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.capability_contracts import (
    CapabilityOutcome,
    ErrorRule,
    catchable_exceptions,
    match_rule,
)


def rest_response(outcome: CapabilityOutcome, *, status_code: int = 200) -> Response:
    """Serve a capability outcome as a REST response."""
    return JSONResponse(outcome.result, status_code=status_code)


def translate_domain_errors(
    rules: tuple[ErrorRule, ...],
) -> Callable[[Callable[..., Awaitable[Response]]], Callable[..., Awaitable[Response]]]:
    """Translate only a domain's declared expected failures at the REST boundary."""
    catchable = catchable_exceptions(rules)

    def decorator(
        handler: Callable[..., Awaitable[Response]],
    ) -> Callable[..., Awaitable[Response]]:
        @functools.wraps(handler)
        async def translated(
            *arguments: object,
            **keyword_arguments: object,
        ) -> Response:
            try:
                return await handler(*arguments, **keyword_arguments)
            except catchable as error:
                rule = match_rule(rules, error)
                detail = rule.detail if rule.detail is not None else str(error)
                return JSONResponse({"detail": detail}, status_code=rule.status)

        return translated

    return decorator


def bind_params(
    execute: Callable[..., Awaitable[CapabilityOutcome]],
) -> Callable[[Request, BaseModel], Awaitable[CapabilityOutcome]]:
    """Bind fields from a validated params model onto a capability execute."""

    async def handler(request: Request, params: BaseModel) -> CapabilityOutcome:
        fields = {name: getattr(params, name) for name in type(params).model_fields}
        return await execute(request, **fields)

    return handler


__all__ = ["bind_params", "rest_response", "translate_domain_errors"]
