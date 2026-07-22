"""The per-consumer proposal-action registry (mirrors `tether.tool_registry`).

A Proposal is an explicitly composed set of typed actions (ADR 0014). Each
action carries a `kind` (e.g. `gmail.archive`); every consumer registers, per
kind, a Pydantic **params model** (validated when the proposal is composed and
again when the action executes), an idempotent async **executor**, and a UI
**rendering hint**. Registration follows the same shape as the `ToolSpec`
registry: each consumer owns a `*_ACTION_SPECS` tuple and `all_action_specs()`
concatenates them centrally, so `ProposalService` can validate params at propose
time, match autonomy grants, and dispatch executors — all from one table.

`ActionContext` is the bundle of dependencies an executor reaches through (a
`GmailClient` for the Gmail consumer, plus the active request logger). It is
built once with the static handles and re-stamped with the per-run logger by
`ProposalService.execute`, so executors stay thin closures over their client.

>>> from pydantic import BaseModel
>>> class ArchiveParams(BaseModel):
...     message_id: str
>>> async def archive(params: BaseModel, context: ActionContext) -> ActionResult:
...     return ActionResult(outcome="succeeded")
>>> spec = ActionSpec("gmail.archive", ArchiveParams, archive, ui_hint="gmail.archive")
>>> build_action_registry([spec])["gmail.archive"].ui_hint
'gmail.archive'
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from tether.gmail import GmailClient
    from tether.logging import Logger

type ActionOutcome = Literal["succeeded", "failed", "skipped"]
"""The terminal result of running one action's executor."""


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an executor returns: a terminal outcome plus optional detail.

    `skipped` is the fail-soft outcome for a stale target — already gone, or
    already in the desired state — so an idempotent re-run of an interrupted
    batch resolves cleanly rather than erroring.

    >>> ActionResult(outcome="skipped", detail="already archived").outcome
    'skipped'
    """

    outcome: ActionOutcome
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ActionContext:
    """The dependency bundle a proposal-action executor runs against.

    `ProposalService` is constructed with the static handles (the `GmailClient`,
    wired in Phase B; `None` until then) and re-stamps `logger` per run before
    dispatching, so an executor can reach both without threading them through
    the service's own signatures. Typed `object`-free via a `TYPE_CHECKING`
    import so this module never imports the Gmail consumer at runtime.
    """

    gmail_client: GmailClient | None = None
    logger: Logger | None = None


class ActionExecutor(Protocol):
    """An idempotent async executor for one action kind.

    Idempotency is a hard contract (ADR 0014): `executing` can crash mid-batch,
    so a safe re-run of the remaining approved actions — with an already-done
    target resolving `skipped`, not error — is what makes crash-resume correct.
    """

    def __call__(
        self, params: BaseModel, context: ActionContext
    ) -> Awaitable[ActionResult]:
        """Run the action against `context`, returning its terminal outcome."""
        ...


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One action kind: its name, params model, executor, and UI hint.

    The single source of truth for a kind. `params_model` types the action at
    the seam (JSON at rest); `executor` carries it out; `ui_hint` tells the
    Proposals panel how to render it.
    """

    kind: str
    params_model: type[BaseModel]
    executor: ActionExecutor
    ui_hint: str = field(default="")


def build_action_registry(specs: Iterable[ActionSpec]) -> dict[str, ActionSpec]:
    """Index action specs by kind, rejecting a duplicate registration.

    A duplicated `kind` is a wiring bug — two consumers claiming the same action
    name — and must fail loudly at construction, not silently shadow one.

    >>> from pydantic import BaseModel
    >>> async def run(params: BaseModel, context: ActionContext) -> ActionResult:
    ...     return ActionResult(outcome="succeeded")
    >>> class P(BaseModel):
    ...     pass
    >>> registry = build_action_registry([ActionSpec("a.b", P, run)])
    >>> sorted(registry)
    ['a.b']
    """
    registry: dict[str, ActionSpec] = {}
    for spec in specs:
        if spec.kind in registry:
            message = f"duplicate action kind: {spec.kind!r}"
            raise ValueError(message)
        registry[spec.kind] = spec
    return registry


def all_action_specs() -> tuple[ActionSpec, ...]:
    """Every registered action spec, in canonical consumer order.

    The first consumer is Gmail hygiene (#199); its `*GMAIL_ACTION_SPECS` join
    here the same way each `*_TOOL_SPECS` tuple joins `all_tool_specs()`. The
    import is function-local to break the cycle `gmail_actions` -> this module
    (for `ActionSpec`) would otherwise create at import time.

    >>> {spec.kind for spec in all_action_specs()} >= {"gmail.archive"}
    True
    """
    # Loaded lazily through importlib to break the action_registry <->
    # gmail_actions import cycle: `gmail_actions` imports the `ActionSpec` types
    # from this module, so a direct import here — even function-local — is a
    # static cycle. The consumer is resolved on first call, after both modules
    # are fully defined.
    specs = importlib.import_module("tether.gmail_actions").GMAIL_ACTION_SPECS
    return cast("tuple[ActionSpec, ...]", specs)
