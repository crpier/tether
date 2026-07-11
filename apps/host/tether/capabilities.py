"""Shared vocabulary for per-capability descriptors (issue #139).

Every domain capability is exposed twice — a public REST route and a loopback
`/internal/tools/*` endpoint — and the two adapters used to be shallow copies
of each other. The pieces both surfaces share now live once, next to the
domain, in a `*_capabilities` module: the execute functions (one service call
plus its Read-model rendering), the detached-reference builder, and the
domain→code map. This module holds the machinery those descriptors are built
from:

* `CapabilityOutcome` — what an execute returns before either surface shapes
  it: a JSON-ready `result` plus the envelope-only metadata.
* `ErrorRule` — one domain failure translated onto both surfaces at once: an
  envelope `code` for the tool seam and a `status` (with optional fixed
  `detail`) for REST.
* `translate_domain_errors` / `rest_response` — the REST derivation.
* `bind_params` / `match_rule` — the tool derivation, consumed by
  `tether.tools.ToolEndpoint`.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.bucket_items import BucketItemProvenance
from tether.memories import MemoryProvenance
from tether.youtube import CacheMeta, QuotaMeta

type ToolErrorCode = Literal[
    "invalid_input", "not_found", "conflict", "quota_exceeded", "upstream_error"
]


@dataclass(frozen=True, slots=True)
class ErrorRule:
    """One domain failure translated onto both HTTP surfaces.

    `code` is the tool envelope's error code and `status` the REST status code
    for the same failure. `detail` fixes the REST body's detail message; left
    `None`, the REST detail is the exception's own message. Envelope messages
    are the exception's message, except absence, which is always the flat
    "not found" so no identifier detail leaks to the model.
    """

    exceptions: tuple[type[Exception], ...]
    code: ToolErrorCode
    status: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """What a capability's execute returns, before either surface shapes it.

    `result` is the JSON-ready payload both surfaces serve verbatim. The other
    fields ride only on the tool envelope: `provenance` for a single row's
    provenance, `quota`/`cache` for capabilities fronting a quota-metered API.
    """

    result: Any
    provenance: MemoryProvenance | BucketItemProvenance | None = None
    quota: QuotaMeta | None = None
    cache: CacheMeta | None = None


def catchable_exceptions(rules: tuple[ErrorRule, ...]) -> tuple[type[Exception], ...]:
    """Flatten a rule table into the exception tuple an `except` clause takes."""
    return tuple(dict.fromkeys(exc for rule in rules for exc in rule.exceptions))


def match_rule(rules: tuple[ErrorRule, ...], error: Exception) -> ErrorRule:
    """Return the first rule naming `error`; only called under its `except`."""
    for rule in rules:
        if isinstance(error, rule.exceptions):
            return rule
    raise error


def rest_response(outcome: CapabilityOutcome, *, status_code: int = 200) -> Response:
    """Serve a capability outcome as the REST surface's JSON body."""
    return JSONResponse(outcome.result, status_code=status_code)


def translate_domain_errors(
    rules: tuple[ErrorRule, ...],
) -> Callable[[Callable[..., Awaitable[Response]]], Callable[..., Awaitable[Response]]]:
    """Map a domain's failures onto REST status codes at the route boundary.

    The decorator catches exactly the exceptions the rule table names; anything
    else propagates. Wrapping the handler keeps each route body focused on the
    happy path.
    """
    catchable = catchable_exceptions(rules)

    def decorator(
        handler: Callable[..., Awaitable[Response]],
    ) -> Callable[..., Awaitable[Response]]:
        @functools.wraps(handler)
        async def translated(*arguments: object) -> Response:
            try:
                return await handler(*arguments)
            except catchable as error:
                rule = match_rule(rules, error)
                detail = rule.detail if rule.detail is not None else str(error)
                return JSONResponse({"detail": detail}, status_code=rule.status)

        return translated

    return decorator


def bind_params(
    execute: Callable[..., Awaitable[CapabilityOutcome]],
) -> Callable[[Request, BaseModel], Awaitable[CapabilityOutcome]]:
    """Adapt an execute to a tool handler by splatting params fields as kwargs.

    Fits whenever a tool's params model names its fields after the execute's
    keyword arguments — the common case; tools that reshape their params (e.g.
    projecting a `TriggerSpec`) keep a small named binding instead.
    """

    async def handler(request: Request, params: BaseModel) -> CapabilityOutcome:
        fields = {name: getattr(params, name) for name in type(params).model_fields}
        return await execute(request, **fields)

    return handler
