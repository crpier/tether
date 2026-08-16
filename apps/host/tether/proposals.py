"""Proposal domain: explicitly composed, host-executed action sets.

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

>>> service = ProposalService(
...     database=database,
...     autonomy_policy=autonomy_service,
...     execution=proposal_executor,
... )
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
from dataclasses import dataclass
from datetime import datetime

from pydantic import UUID7
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.notifications import NotificationDraft, NotificationService
from tether.proposal_autonomy import ActionCategory, ProposalAutonomyPolicy
from tether.proposal_errors import (
    InvalidActionError,
    ProposalConflictError,
    ProposalNotFoundError,
    ProposalStateError,
)
from tether.proposal_execution import ProposalExecution
from tether.proposal_store import Proposal, ProposalAction, ProposalState
from tether.structured_logging import Logger


@dataclass(frozen=True, slots=True)
class ActionDraft:
    """One action to compose into a proposal: a kind, a scope, and raw params.

    `display` is an optional consumer-supplied human-readable one-line summary,
    kept separate from `params` (the typed executor contract) so
    rendering text never leaks into what the executor validates. When absent the
    panel falls back to rendering the kind and params.
    """

    kind: str
    scope: str | None
    params: dict[str, object]
    display: str | None = None


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
class ProposalCounts:
    """Queue and history totals for the Proposals tab strip."""

    decided: int
    pending: int


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


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


class ProposalService:
    """Orchestrate the human-facing Proposal lifecycle.

    Creation, reads, approval, and rejection stay here. Action validation and
    resumable execution are delegated to the explicitly composed execution
    service.
    """

    def __init__(
        self,
        database: Database,
        *,
        autonomy_policy: ProposalAutonomyPolicy,
        execution: ProposalExecution,
        event_publisher: EventPublisher | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        self.database: Database = database
        self.autonomy_policy: ProposalAutonomyPolicy = autonomy_policy
        self.execution: ProposalExecution = execution
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
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
        self.execution.validate_actions(draft.actions)
        proposal_id = await self._insert(draft, producing_run_id)
        _info(
            logger,
            "Proposal composed",
            proposal_id=str(proposal_id),
            consumer=draft.consumer,
            action_count=len(draft.actions),
        )
        actions = await self._fetch_actions(proposal_id)
        if await self.autonomy_policy.covers_all(
            [ActionCategory(kind=action.kind, scope=action.scope) for action in actions]
        ):
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
                            display=action.display,
                            disposition="approved",
                        )
                    )
                )
            return proposal.id

        async with self.database.transaction(mode="immediate") as tx:
            return await _create(tx)

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

    async def counts(self, *, logger: Logger) -> ProposalCounts:
        """Count queue and history proposals without loading actions."""
        _debug(logger, "Counting proposals")
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(
                select(Proposal.state, Proposal.id.count())
                .all()
                .group_by(Proposal.state)
            )
        pending = 0
        decided = 0
        for state, count in rows:
            if state == "pending":
                pending = count
            else:
                decided += count
        return ProposalCounts(decided=decided, pending=pending)

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

        async with self.database.transaction(mode="immediate") as tx:
            return await _approve(tx)

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

        async with self.database.transaction(mode="immediate") as tx:
            proposal = await _reject(tx)
        actions = await self._fetch_actions(proposal_ref.id)
        revocable_grant_ids = await self.autonomy_policy.revocable_grant_ids(
            [ActionCategory(kind=action.kind, scope=action.scope) for action in actions]
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return RejectionOutcome(
            proposal=ProposalView(proposal=proposal, actions=actions),
            revocable_grant_ids=revocable_grant_ids,
        )

    # --- execute ---------------------------------------------------------

    async def execute(
        self, proposal_id: UUID7, *, now: datetime, logger: Logger
    ) -> ProposalView:
        """Execute or resume an approved Proposal, then return its settled view."""
        await self.execution.execute(proposal_id, now=now, logger=logger)
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return await self.get(proposal_id)

    # --- helpers ---------------------------------------------------------

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
