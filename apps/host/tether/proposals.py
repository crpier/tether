"""Proposal domain: explicitly composed, host-executed action sets (ADR 0014).

A Proposal gates an agent-initiated *set* of typed, consequential actions. The
agent composes one with `propose` (consumer, title, summary, an ordered list of
typed actions); the host stores it first-class, and on approval **the host**
executes it through per-kind executors — no agent is in the loop at execution
time, so a batch outlives the session that proposed it.

Trust is granted per `(kind, scope)` category through an `AutonomyGrant`.
Matching is **fail-closed**: an action executes automatically only when every
action in its proposal is covered by a live grant; any uncovered action queues
the *whole* proposal for human review — the system never splits a batch. Grant
state is read live on every evaluation, never cached, so a revocation applies to
the very next proposal.

Lifecycle: `pending → approved | rejected`, then `approved → executing →
executed | failed`. Approval can be partial (the human deselects actions before
approving). Per-action outcomes (`succeeded | failed | skipped`) are **appended
as they happen and never overwritten**, which is what makes an interrupted
`executing` batch safe to re-run: already-resolved actions are skipped and only
NULL-outcome approved actions run again.

>>> service = ProposalService(database=database, tracer=tracer)
>>> creation = await service.create(
...     ProposalDraft(
...         consumer="gmail",
...         title="Archive 3 newsletters",
...         summary="...",
...         actions=[ActionDraft(kind="gmail.archive", scope=None, params={})],
...     ),
...     now=datetime.now(UTC),
...     logger=logger,
... )
>>> creation.auto_executed
False
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import ClassVar, Literal
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt, ValidationError
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.action_registry import (
    ActionContext,
    ActionResult,
    ActionSpec,
    all_action_specs,
    build_action_registry,
)
from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger
from tether.notifications import NotificationDraft, NotificationService

type ProposalState = Literal[
    "pending", "approved", "executing", "executed", "failed", "rejected"
]
"""A proposal's lifecycle state; `executing` may be long-lived."""

type ActionDisposition = Literal["approved", "deselected"]
"""Whether an action was kept (`approved`) or unticked before approval."""

type ActionOutcome = Literal["succeeded", "failed", "skipped"]
"""One action's terminal execution result; `skipped` is the fail-soft outcome."""

_APPROVED_STATES: frozenset[str] = frozenset({"approved", "executing", "executed"})
"""States a proposal reaches by being approved, for calibration accounting."""


class ProposalNotFoundError(Exception):
    """Raised when an operation targets a proposal that does not exist."""


class ProposalConflictError(Exception):
    """Raised when a stale observed version cannot accept the mutation.

    A version that has moved on since it was read, not absence.
    """


class ProposalStateError(Exception):
    """Raised on an illegal lifecycle transition (e.g. approve a non-pending)."""


class InvalidActionError(Exception):
    """Raised when an action names an unknown kind or fails its params model."""


class Proposal[S = Pending](Model[S, "Proposal[Fetched]"]):
    """An explicitly composed action set, plus its lifecycle state."""

    id: Proposal.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    consumer: Proposal.Col[str] = Text()
    """The registering consumer (e.g. `gmail`) that produced this proposal."""
    title: Proposal.Col[str] = Text()
    summary: Proposal.Col[str] = Text()
    producing_run_id: Proposal.Col[str | None] = Text(default=None, nullable=True)
    """Provenance: the agent run that produced this proposal, when known."""
    state: Proposal.Col[ProposalState] = Text()
    rejection_reason: Proposal.Col[str | None] = Text(default=None, nullable=True)
    version: Proposal.Col[PositiveInt] = Integer(default=1)
    """Optimistic-concurrency version, bumped on every lifecycle transition."""
    created_at: Proposal.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: Proposal.GenCol[datetime] = Text(default=CurrentTimestamp)
    decided_at: Proposal.Col[datetime | None] = Text(default=None, nullable=True)
    """Stamped when the proposal leaves `pending` (approved or rejected)."""

    __indexes__: ClassVar = [Index(state, created_at)]


class ProposalAction[S = Pending](Model[S, "ProposalAction[Fetched]"]):
    """One typed action within a proposal: typed at the seam, JSON at rest."""

    id: ProposalAction.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    proposal_id: ProposalAction.Col[str] = Text()
    """The owning proposal's id (a logical foreign key)."""
    seq: ProposalAction.Col[int] = Integer()
    """Position within the proposal; execution follows ascending `seq`."""
    kind: ProposalAction.Col[str] = Text()
    scope: ProposalAction.Col[str | None] = Text(default=None, nullable=True)
    params_json: ProposalAction.Col[str] = Text()
    """The action's params as JSON; re-validated against the kind at execute."""
    disposition: ProposalAction.Col[ActionDisposition] = Text()
    """`approved` (kept) or `deselected` (unticked before approval)."""
    outcome: ProposalAction.Col[ActionOutcome | None] = Text(
        default=None, nullable=True
    )
    """Terminal execution result; append-only, never overwritten once set."""
    outcome_detail: ProposalAction.Col[str | None] = Text(default=None, nullable=True)
    executed_at: ProposalAction.Col[datetime | None] = Text(default=None, nullable=True)

    __indexes__: ClassVar = [Index(proposal_id, seq)]


class AutonomyGrant[S = Pending](Model[S, "AutonomyGrant[Fetched]"]):
    """A live trust grant for a `(kind, scope)` category; append-only ledger.

    A bare-kind grant (`scope IS NULL`) covers every scope for that kind. Rows
    are stamped `revoked_at`, never deleted, so a re-grant is a new row and the
    table doubles as a permanent trust-history log.
    """

    id: AutonomyGrant.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    kind: AutonomyGrant.Col[str] = Text()
    scope: AutonomyGrant.Col[str | None] = Text(default=None, nullable=True)
    granted_at: AutonomyGrant.GenCol[datetime] = Text(default=CurrentTimestamp)
    revoked_at: AutonomyGrant.Col[datetime | None] = Text(default=None, nullable=True)


@dataclass(frozen=True, slots=True)
class ActionDraft:
    """One action to compose into a proposal: a kind, a scope, and raw params."""

    kind: str
    scope: str | None
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """The content of one proposal to compose: its metadata plus ordered actions."""

    consumer: str
    title: str
    summary: str
    actions: list[ActionDraft]


@dataclass(frozen=True, slots=True)
class ProposalView:
    """A proposal bundled with its actions, in `seq` order."""

    proposal: Proposal[Fetched]
    actions: list[ProposalAction[Fetched]]


@dataclass(frozen=True, slots=True)
class ProposalCreation:
    """The result of composing a proposal: the view plus whether it auto-executed.

    `auto_executed` is true when every action was grant-covered and the host ran
    the batch immediately; false when it queued for human review.
    """

    proposal: ProposalView
    auto_executed: bool


@dataclass(frozen=True, slots=True)
class RejectionOutcome:
    """A rejected proposal plus the live grants that would cover its actions.

    A non-empty `revocable_grant_ids` lets the UI *offer* revocation — rejecting
    in an already-granted category is a signal the human may want to revoke it —
    but revocation itself is always a separate, explicit human act.
    """

    proposal: ProposalView
    revocable_grant_ids: list[UUID7]


@dataclass(frozen=True, slots=True)
class GrantSuggestion:
    """Read-time calibration for one ungranted `(kind, scope)` category."""

    kind: str
    scope: str | None
    seen: int
    approved: int
    rejected: int
    edited: int
    last_rejection: datetime | None


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


class ProposalService:
    """Capability surface for Proposals, over a snekql database.

    Owns the human-facing lifecycle (create / list / get / approve / reject),
    the autonomy grant ledger (grant / revoke / list / suggestions), and the
    host executor loop (`execute`). The executor loop is idempotent and
    re-runnable, so an interrupted `executing` batch resumes safely.
    """

    def __init__(  # noqa: PLR0913 - each collaborator is an independent injected dependency
        self,
        database: Database,
        tracer: Tracer,
        *,
        event_publisher: EventPublisher | None = None,
        action_registry: dict[str, ActionSpec] | None = None,
        action_context: ActionContext | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        self.database: Database = database
        self.tracer: Tracer = tracer
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.action_registry: dict[str, ActionSpec] = (
            action_registry
            if action_registry is not None
            else build_action_registry(all_action_specs())
        )
        self.action_context: ActionContext = action_context or ActionContext()
        self.notification_service: NotificationService | None = notification_service

    # --- create + auto-execute -------------------------------------------

    async def create(
        self,
        draft: ProposalDraft,
        *,
        producing_run_id: str | None = None,
        now: datetime,
        logger: Logger,
    ) -> ProposalCreation:
        """Compose a proposal, auto-executing it iff every action is granted.

        Every action's params are validated against its kind before any write
        (unknown kind or bad params -> `InvalidActionError`). Grant coverage is
        evaluated live afterwards: full coverage transitions the proposal to
        approved and runs the executor loop immediately; any gap leaves it
        pending, records a notification, and queues it for human review.
        """
        if not draft.actions:
            message = "a proposal requires at least one action"
            raise InvalidActionError(message)
        self._validate_actions(draft.actions)
        proposal_id = await self._insert(draft, producing_run_id)
        _info(
            logger,
            "Proposal composed",
            proposal_id=str(proposal_id),
            consumer=draft.consumer,
            action_count=len(draft.actions),
        )
        grants = await self._live_grants()
        actions = await self._fetch_actions(proposal_id)
        if all(self._is_covered(a.kind, a.scope, grants) for a in actions):
            proposal = await self._fetch(proposal_id)
            _ = await self._do_approve(
                proposal, deselected=set(), now=now, logger=logger
            )
            view = await self.execute(proposal_id, now=now, logger=logger)
            return ProposalCreation(proposal=view, auto_executed=True)
        if self.notification_service is not None:
            _ = await self.notification_service.record(
                NotificationDraft(body=draft.summary, source_label=draft.title)
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return ProposalCreation(
            proposal=await self.get(proposal_id), auto_executed=False
        )

    def _validate_actions(self, actions: list[ActionDraft]) -> None:
        """Validate every action's params against its kind, or raise."""
        for action in actions:
            spec = self.action_registry.get(action.kind)
            if spec is None:
                message = f"unknown action kind: {action.kind!r}"
                raise InvalidActionError(message)
            try:
                _ = spec.params_model.model_validate(action.params)
            except ValidationError as error:
                message = f"invalid params for {action.kind!r}: {error}"
                raise InvalidActionError(message) from error

    async def _insert(
        self, draft: ProposalDraft, producing_run_id: str | None
    ) -> UUID7:
        """Insert the proposal and its actions in one transaction; return its id."""

        async def _create(tx: Transaction) -> UUID7:
            proposal = await tx.execute(
                insert(
                    Proposal(
                        consumer=draft.consumer,
                        title=draft.title,
                        summary=draft.summary,
                        producing_run_id=producing_run_id,
                        state="pending",
                    )
                ).returning()
            )
            for seq, action in enumerate(draft.actions):
                _ = await tx.execute(
                    insert(
                        ProposalAction(
                            proposal_id=str(proposal.id),
                            seq=seq,
                            kind=action.kind,
                            scope=action.scope,
                            params_json=json.dumps(action.params),
                            disposition="approved",
                        )
                    )
                )
            return proposal.id

        return await run_in_transaction(self.database, _create)

    # --- read ------------------------------------------------------------

    async def list_proposals(
        self,
        *,
        state: ProposalState | None = None,
        limit: int | None = None,
        logger: Logger,
    ) -> list[ProposalView]:
        """List proposals newest first, each bundled with its actions.

        `state`, when given, filters to that lifecycle state; `limit` caps the
        rows (assistant-facing callers pass a bound).
        """
        _debug(logger, "Listing proposals", state=state)
        base = select(Proposal)
        filtered = (
            base.where(Proposal.state.eq(state)) if state is not None else base.all()
        )
        query = filtered.order_by(Proposal.created_at.desc()).order_by(
            Proposal.id.desc()
        )
        if limit is not None:
            query = query.limit(limit)
        async with self.database.transaction() as tx:
            proposals = await tx.fetch_all(query)
            return [
                ProposalView(proposal=p, actions=await self._fetch_actions(p.id, tx=tx))
                for p in proposals
            ]

    async def get(self, proposal_id: UUID7) -> ProposalView:
        """Fetch one proposal bundled with its actions, or raise when absent."""
        async with self.database.transaction() as tx:
            proposal = await self._fetch(proposal_id, tx=tx)
            return ProposalView(
                proposal=proposal, actions=await self._fetch_actions(proposal_id, tx=tx)
            )

    # --- approve / reject ------------------------------------------------

    async def approve(
        self,
        proposal_ref: Proposal[Fetched],
        *,
        deselected_action_ids: set[UUID7],
        now: datetime,
        logger: Logger,
    ) -> ProposalView:
        """Approve a pending proposal at an observed version, then execute it.

        Deselected actions are recorded `deselected` and never run; the rest are
        approved. A stale version conflicts; a non-pending proposal is a state
        error. Approval flows straight into the host executor loop.
        """
        _debug(
            logger,
            "Approving proposal",
            proposal_id=str(proposal_ref.id),
            observed_version=proposal_ref.version,
            deselected=len(deselected_action_ids),
        )
        _ = await self._do_approve(
            proposal_ref, deselected=deselected_action_ids, now=now, logger=logger
        )
        return await self.execute(proposal_ref.id, now=now, logger=logger)

    async def _do_approve(
        self,
        proposal_ref: Proposal[Fetched],
        *,
        deselected: set[UUID7],
        now: datetime,
        logger: Logger,
    ) -> Proposal[Fetched]:
        """Version-checked `pending -> approved`, stamping deselected actions."""

        async def _approve(tx: Transaction) -> Proposal[Fetched]:
            for action_id in deselected:
                _ = await tx.execute(
                    update(ProposalAction)
                    .set(ProposalAction.disposition.to("deselected"))
                    .where(ProposalAction.id.eq(action_id))
                    .where(ProposalAction.proposal_id.eq(str(proposal_ref.id)))
                )
            matched = await tx.execute(
                update(Proposal)
                .set(Proposal.state.to("approved"))
                .set(Proposal.decided_at.to(now))
                .set(Proposal.version.to(proposal_ref.version + 1))
                .set(Proposal.updated_at.to(CurrentTimestamp))
                .where(Proposal.id.eq(proposal_ref.id))
                .where(Proposal.state.eq("pending"))
                .where(Proposal.version.eq(proposal_ref.version))
            )
            fresh = await self._fetch(proposal_ref.id, tx=tx)
            if matched == 0:
                self._raise_transition_failure(proposal_ref, fresh, logger=logger)
            return fresh

        return await run_in_transaction(self.database, _approve)

    async def reject(
        self,
        proposal_ref: Proposal[Fetched],
        *,
        reason: str | None,
        now: datetime,
        logger: Logger,
    ) -> RejectionOutcome:
        """Reject a pending proposal at an observed version (terminal).

        Records the optional free-text `reason` and returns the live grants that
        cover this proposal's actions, so the UI can *offer* revocation. A stale
        version conflicts; a non-pending proposal is a state error.
        """
        _debug(
            logger,
            "Rejecting proposal",
            proposal_id=str(proposal_ref.id),
            observed_version=proposal_ref.version,
        )

        async def _reject(tx: Transaction) -> Proposal[Fetched]:
            matched = await tx.execute(
                update(Proposal)
                .set(Proposal.state.to("rejected"))
                .set(Proposal.rejection_reason.to(reason))
                .set(Proposal.decided_at.to(now))
                .set(Proposal.version.to(proposal_ref.version + 1))
                .set(Proposal.updated_at.to(CurrentTimestamp))
                .where(Proposal.id.eq(proposal_ref.id))
                .where(Proposal.state.eq("pending"))
                .where(Proposal.version.eq(proposal_ref.version))
            )
            fresh = await self._fetch(proposal_ref.id, tx=tx)
            if matched == 0:
                self._raise_transition_failure(proposal_ref, fresh, logger=logger)
            return fresh

        proposal = await run_in_transaction(self.database, _reject)
        actions = await self._fetch_actions(proposal_ref.id)
        grants = await self._live_grants()
        revocable = sorted(
            {
                grant.id
                for action in actions
                for grant in grants
                if self._grant_matches(grant, action.kind, action.scope)
            }
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return RejectionOutcome(
            proposal=ProposalView(proposal=proposal, actions=actions),
            revocable_grant_ids=list(revocable),
        )

    # --- execute (host executor loop) ------------------------------------

    async def execute(
        self, proposal_id: UUID7, *, now: datetime, logger: Logger
    ) -> ProposalView:
        """Run the approved actions of a proposal; idempotent and re-runnable.

        Transitions `approved -> executing`, then runs every approved action with
        a NULL outcome in `seq` order — already-resolved actions are skipped, so
        a crash-interrupted batch resumes safely. Outcomes are appended, never
        overwritten. When the batch settles the proposal becomes `failed` if any
        approved action failed, else `executed`. Tolerant of being called while
        already `executing`.
        """
        proposal = await self._enter_executing(proposal_id)
        _info(
            logger,
            "Executing proposal",
            proposal_id=str(proposal_id),
            version=proposal.version,
        )
        context = replace(self.action_context, logger=logger)
        for action in await self._fetch_actions(proposal_id):
            if action.disposition != "approved" or action.outcome is not None:
                continue
            result = await self._run_action(action, context)
            await self._append_outcome(action.id, result, now)
        _ = await self._settle(proposal_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return await self.get(proposal_id)

    async def _enter_executing(self, proposal_id: UUID7) -> Proposal[Fetched]:
        """Transition `approved -> executing`, or accept an in-flight `executing`."""

        async def _enter(tx: Transaction) -> Proposal[Fetched]:
            proposal = await self._fetch(proposal_id, tx=tx)
            if proposal.state not in ("approved", "executing"):
                message = f"proposal {proposal_id} is {proposal.state}, not approved/executing"
                raise ProposalStateError(message)
            if proposal.state == "approved":
                _ = await tx.execute(
                    update(Proposal)
                    .set(Proposal.state.to("executing"))
                    .set(Proposal.version.to(proposal.version + 1))
                    .set(Proposal.updated_at.to(CurrentTimestamp))
                    .where(Proposal.id.eq(proposal_id))
                    .where(Proposal.state.eq("approved"))
                )
            return await self._fetch(proposal_id, tx=tx)

        return await run_in_transaction(self.database, _enter)

    async def _run_action(
        self, action: ProposalAction[Fetched], context: ActionContext
    ) -> ActionResult:
        """Dispatch one action to its kind's executor, failing soft on error."""
        spec = self.action_registry.get(action.kind)
        if spec is None:
            return ActionResult(outcome="failed", detail="unknown action kind")
        try:
            params = spec.params_model.model_validate_json(action.params_json)
            return await spec.executor(params, context)
        except Exception as error:
            return ActionResult(outcome="failed", detail=str(error))

    async def _append_outcome(
        self, action_id: UUID7, result: ActionResult, now: datetime
    ) -> None:
        """Append an outcome, but only onto a still-NULL outcome (append-only)."""

        async def _append(tx: Transaction) -> int:
            return await tx.execute(
                update(ProposalAction)
                .set(ProposalAction.outcome.to(result.outcome))
                .set(ProposalAction.outcome_detail.to(result.detail))
                .set(ProposalAction.executed_at.to(now))
                .where(ProposalAction.id.eq(action_id))
                .where(ProposalAction.outcome.is_null())
            )

        _ = await run_in_transaction(self.database, _append)

    async def _settle(self, proposal_id: UUID7) -> Proposal[Fetched]:
        """Settle `executing -> executed | failed` from the action outcomes."""

        async def _finish(tx: Transaction) -> Proposal[Fetched]:
            proposal = await self._fetch(proposal_id, tx=tx)
            actions = await self._fetch_actions(proposal_id, tx=tx)
            any_failed = any(
                a.outcome == "failed" for a in actions if a.disposition == "approved"
            )
            final_state: ProposalState = "failed" if any_failed else "executed"
            _ = await tx.execute(
                update(Proposal)
                .set(Proposal.state.to(final_state))
                .set(Proposal.version.to(proposal.version + 1))
                .set(Proposal.updated_at.to(CurrentTimestamp))
                .where(Proposal.id.eq(proposal_id))
                .where(Proposal.state.eq("executing"))
            )
            return await self._fetch(proposal_id, tx=tx)

        return await run_in_transaction(self.database, _finish)

    # --- grants ----------------------------------------------------------

    async def grant(
        self, kind: str, scope: str | None, *, now: datetime
    ) -> AutonomyGrant[Fetched]:
        """Grant autonomy for a `(kind, scope)` category (a new ledger row)."""
        _ = now

        async def _grant(tx: Transaction) -> AutonomyGrant[Fetched]:
            return await tx.execute(
                insert(AutonomyGrant(kind=kind, scope=scope)).returning()
            )

        granted = await run_in_transaction(self.database, _grant)
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return granted

    async def revoke(self, grant_id: UUID7, *, now: datetime) -> None:
        """Revoke a grant convergently; an absent/already-revoked id is a no-op."""

        async def _revoke(tx: Transaction) -> int:
            return await tx.execute(
                update(AutonomyGrant)
                .set(AutonomyGrant.revoked_at.to(now))
                .where(AutonomyGrant.id.eq(grant_id))
                .where(AutonomyGrant.revoked_at.is_null())
            )

        matched = await run_in_transaction(self.database, _revoke)
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))

    async def list_grants(self) -> list[AutonomyGrant[Fetched]]:
        """List live (unrevoked) grants, newest first."""
        return await self._live_grants()

    async def calibration_stats(self) -> list[GrantSuggestion]:
        """Compute read-time grant suggestions from proposal history.

        Groups every action by `(kind, scope)` and, over the joined proposals,
        counts how often the category was seen, approved, rejected, and edited,
        plus the most recent rejection. Only ungranted categories surface, and
        nothing is stored (recomputed on read, in the spirit of ADR 0006).
        """
        async with self.database.transaction() as tx:
            proposals = await tx.fetch_all(select(Proposal).all())
            actions = await tx.fetch_all(select(ProposalAction).all())
        by_id = {str(p.id): p for p in proposals}
        deselected_proposals = {
            a.proposal_id for a in actions if a.disposition == "deselected"
        }
        aggregates: dict[tuple[str, str | None], _Aggregate] = {}
        for action in actions:
            proposal = by_id.get(action.proposal_id)
            if proposal is None:
                continue
            aggregate = aggregates.setdefault((action.kind, action.scope), _Aggregate())
            aggregate.observe(
                action,
                proposal,
                edited=action.proposal_id in deselected_proposals,
            )
        grants = await self._live_grants()
        return [
            aggregate.to_suggestion(kind, scope)
            for (kind, scope), aggregate in aggregates.items()
            if not self._is_covered(kind, scope, grants)
        ]

    # --- helpers ---------------------------------------------------------

    async def _live_grants(self) -> list[AutonomyGrant[Fetched]]:
        """Read every live grant fresh from the database (never cached)."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(AutonomyGrant)
                .where(AutonomyGrant.revoked_at.is_null())
                .order_by(AutonomyGrant.granted_at.desc())
                .order_by(AutonomyGrant.id.desc())
            )

    @staticmethod
    def _grant_matches(
        grant: AutonomyGrant[Fetched], kind: str, scope: str | None
    ) -> bool:
        """A grant covers an action iff kinds match and its scope is bare or equal."""
        return grant.kind == kind and (grant.scope is None or grant.scope == scope)

    def _is_covered(
        self, kind: str, scope: str | None, grants: list[AutonomyGrant[Fetched]]
    ) -> bool:
        """Fail-closed coverage check for one `(kind, scope)` against live grants."""
        return any(self._grant_matches(grant, kind, scope) for grant in grants)

    async def _fetch(
        self, proposal_id: UUID7, *, tx: Transaction | None = None
    ) -> Proposal[Fetched]:
        """Fetch a proposal by id or raise, optionally within an open transaction."""
        if tx is not None:
            return await self._fetch_in(tx, proposal_id)
        async with self.database.transaction() as own:
            return await self._fetch_in(own, proposal_id)

    @staticmethod
    async def _fetch_in(tx: Transaction, proposal_id: UUID7) -> Proposal[Fetched]:
        """Fetch a proposal within an open transaction or raise."""
        proposal = await tx.fetch_one_or_none(
            select(Proposal).where(Proposal.id.eq(proposal_id))
        )
        if proposal is None:
            raise ProposalNotFoundError(proposal_id)
        return proposal

    async def _fetch_actions(
        self, proposal_id: UUID7, *, tx: Transaction | None = None
    ) -> list[ProposalAction[Fetched]]:
        """Fetch a proposal's actions in `seq` order."""
        query = (
            select(ProposalAction)
            .where(ProposalAction.proposal_id.eq(str(proposal_id)))
            .order_by(ProposalAction.seq.asc())
        )
        if tx is not None:
            return await tx.fetch_all(query)
        async with self.database.transaction() as own:
            return await own.fetch_all(query)

    def _raise_transition_failure(
        self,
        observed: Proposal[Fetched],
        fresh: Proposal[Fetched],
        *,
        logger: Logger,
    ) -> None:
        """Raise conflict for a stale version, else a state error for the transition."""
        if fresh.version != observed.version:
            _debug(
                logger,
                "Proposal version conflict",
                proposal_id=str(observed.id),
                observed_version=observed.version,
                current_version=fresh.version,
            )
            message = (
                f"Tried to act on proposal {observed.id} at version "
                f"{observed.version} but it had version {fresh.version}"
            )
            raise ProposalConflictError(message)
        message = f"proposal {observed.id} is {fresh.state}, not pending"
        raise ProposalStateError(message)


class _Aggregate:
    """Mutable per-`(kind, scope)` calibration accumulator (read-time only)."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.approved: set[str] = set()
        self.rejected: set[str] = set()
        self.edited: set[str] = set()
        self.last_rejection: datetime | None = None

    def observe(
        self,
        action: ProposalAction[Fetched],
        proposal: Proposal[Fetched],
        *,
        edited: bool,
    ) -> None:
        """Fold one action/proposal pair into the running counts."""
        proposal_id = str(proposal.id)
        self.seen.add(proposal_id)
        if edited:
            self.edited.add(proposal_id)
        if proposal.state in _APPROVED_STATES and action.disposition == "approved":
            self.approved.add(proposal_id)
        if proposal.state == "rejected":
            self.rejected.add(proposal_id)
            self.last_rejection = _max_datetime(
                self.last_rejection, _as_utc(proposal.decided_at)
            )

    def to_suggestion(self, kind: str, scope: str | None) -> GrantSuggestion:
        """Render the accumulated counts as a grant suggestion."""
        return GrantSuggestion(
            kind=kind,
            scope=scope,
            seen=len(self.seen),
            approved=len(self.approved),
            rejected=len(self.rejected),
            edited=len(self.edited),
            last_rejection=self.last_rejection,
        )


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a stored timestamp as UTC-aware; SQLite writes naive timestamps."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    """Return the later of two optional datetimes."""
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


async def create_proposal_schema(database: Database) -> None:
    """Create the proposal tables and their indexes on an initialized database.

    Applied as its own ordered migrations after the earlier schemas; each
    scaffolded statement becomes one ordered migration.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_proposal_schema(database)
    """
    migrations = {
        f"030_{label}": sql
        for label, sql in scaffold_sqlite_statements(
            [Proposal, ProposalAction, AutonomyGrant]
        )
    }
    await database.migrate(migrations)
