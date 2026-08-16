"""Proposal action validation and resumable host execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from pydantic import UUID7, ValidationError
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    select,
    update,
)

from tether.action_registry import (
    ActionContext,
    ActionResult,
    ActionSpec,
    all_action_specs,
    build_action_registry,
)
from tether.proposal_errors import (
    InvalidActionError,
    ProposalNotFoundError,
    ProposalStateError,
)
from tether.proposal_store import Proposal, ProposalAction, ProposalState
from tether.structured_logging import Logger


class ActionDraftInput(Protocol):
    """Action fields required for registry validation before persistence."""

    @property
    def kind(self) -> str:
        """Return the registered action kind."""
        ...

    @property
    def params(self) -> dict[str, object]:
        """Return raw parameters validated by the kind's model."""
        ...


class ProposalExecution(Protocol):
    """Action validation and execution required by Proposal orchestration."""

    def validate_actions(self, actions: Sequence[ActionDraftInput]) -> None:
        """Validate action kinds and parameters before persistence."""
        ...

    async def execute(
        self,
        proposal_id: UUID7,
        *,
        now: datetime,
        logger: Logger,
    ) -> None:
        """Execute or resume one approved Proposal."""
        ...


class ProposalExecutor:
    """Validate and execute Proposal actions without an agent in the loop.

    Expected action failures are returned by executors as `ActionResult` values.
    Unexpected defects remain exceptional; the Proposal stays `executing` with
    unresolved outcomes and can be resumed safely.
    """

    def __init__(
        self,
        database: Database,
        *,
        action_registry: dict[str, ActionSpec] | None = None,
        action_context: ActionContext | None = None,
    ) -> None:
        self.database: Database = database
        self.action_registry: dict[str, ActionSpec] = (
            action_registry
            if action_registry is not None
            else build_action_registry(all_action_specs())
        )
        self.action_context: ActionContext = action_context or ActionContext()

    def validate_actions(self, actions: Sequence[ActionDraftInput]) -> None:
        """Validate every action against its registered parameter model."""
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

    async def execute(
        self,
        proposal_id: UUID7,
        *,
        now: datetime,
        logger: Logger,
    ) -> None:
        """Execute unresolved approved actions and settle the Proposal.

        `approved` enters `executing`; an already-`executing` Proposal resumes.
        Resolved and deselected actions are skipped. Outcomes are appended only
        while NULL, so retries cannot overwrite terminal history.
        """
        proposal = await self._enter_executing(proposal_id)
        logger.info(
            "Executing proposal",
            proposal_id=str(proposal_id),
            version=proposal.version,
        )
        context = replace(self.action_context, logger=logger)
        for action in await self._fetch_actions(proposal_id):
            if action.disposition != "approved" or action.outcome is not None:
                continue
            action_result = await self._run_action(action, context)
            await self._append_outcome(action.id, action_result, now)
        await self._settle(proposal_id)

    async def _enter_executing(self, proposal_id: UUID7) -> Proposal[Fetched]:
        """Transition `approved` to `executing`, or resume `executing`."""

        async def _enter(transaction: Transaction) -> Proposal[Fetched]:
            proposal = await self._fetch_in(transaction, proposal_id)
            if proposal.state not in ("approved", "executing"):
                message = (
                    f"proposal {proposal_id} is {proposal.state}, "
                    "not approved/executing"
                )
                raise ProposalStateError(message)
            if proposal.state == "approved":
                _ = await transaction.execute(
                    update(Proposal)
                    .set(Proposal.state.to("executing"))
                    .set(Proposal.version.to(proposal.version + 1))
                    .set(Proposal.updated_at.to(CurrentTimestamp))
                    .where(Proposal.id.eq(proposal_id))
                    .where(Proposal.state.eq("approved"))
                )
            return await self._fetch_in(transaction, proposal_id)

        async with self.database.transaction(mode="immediate") as transaction:
            return await _enter(transaction)

    async def _run_action(
        self,
        action: ProposalAction[Fetched],
        context: ActionContext,
    ) -> ActionResult:
        """Dispatch one action while preserving unexpected defects."""
        spec = self.action_registry.get(action.kind)
        if spec is None:
            return ActionResult(outcome="failed", detail="unknown action kind")
        params = spec.params_model.model_validate_json(action.params_json)
        return await spec.executor(params, context)

    async def _append_outcome(
        self,
        action_id: UUID7,
        action_result: ActionResult,
        now: datetime,
    ) -> None:
        """Append an outcome only while the persisted outcome remains NULL."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(ProposalAction)
                .set(ProposalAction.outcome.to(action_result.outcome))
                .set(ProposalAction.outcome_detail.to(action_result.detail))
                .set(ProposalAction.executed_at.to(now))
                .where(ProposalAction.id.eq(action_id))
                .where(ProposalAction.outcome.is_null())
            )

    async def _settle(self, proposal_id: UUID7) -> None:
        """Settle `executing` from the append-only approved-action outcomes."""
        async with self.database.transaction(mode="immediate") as transaction:
            proposal = await self._fetch_in(transaction, proposal_id)
            actions = await self._fetch_actions(proposal_id, transaction=transaction)
            any_failed = any(
                action.outcome == "failed"
                for action in actions
                if action.disposition == "approved"
            )
            final_state: ProposalState = "failed" if any_failed else "executed"
            _ = await transaction.execute(
                update(Proposal)
                .set(Proposal.state.to(final_state))
                .set(Proposal.version.to(proposal.version + 1))
                .set(Proposal.updated_at.to(CurrentTimestamp))
                .where(Proposal.id.eq(proposal_id))
                .where(Proposal.state.eq("executing"))
            )

    @staticmethod
    async def _fetch_in(
        transaction: Transaction,
        proposal_id: UUID7,
    ) -> Proposal[Fetched]:
        """Fetch one Proposal inside a transaction or preserve not-found identity."""
        proposal = await transaction.fetch_one_or_none(
            select(Proposal).where(Proposal.id.eq(proposal_id))
        )
        if proposal is None:
            raise ProposalNotFoundError(proposal_id)
        return proposal

    async def _fetch_actions(
        self,
        proposal_id: UUID7,
        *,
        transaction: Transaction | None = None,
    ) -> list[ProposalAction[Fetched]]:
        """Fetch persisted actions in execution order."""
        query = (
            select(ProposalAction)
            .where(ProposalAction.proposal_id.eq(str(proposal_id)))
            .order_by(ProposalAction.seq.asc())
        )
        if transaction is not None:
            return await transaction.fetch_all(query)
        async with self.database.transaction() as own_transaction:
            return await own_transaction.fetch_all(query)
