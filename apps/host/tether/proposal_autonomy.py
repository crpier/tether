"""Proposal autonomy grants and calibration policy."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import UUID7
from snekql.sqlite import Database, Fetched, Transaction, insert, select, update

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.proposal_store import AutonomyGrant, Proposal, ProposalAction

_APPROVED_STATES: frozenset[str] = frozenset({"approved", "executing", "executed"})
"""States a proposal reaches by being approved, for calibration accounting."""


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


@dataclass(frozen=True, slots=True)
class ActionCategory:
    """The action identity against which an autonomy grant is matched."""

    kind: str
    scope: str | None


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


class ProposalAutonomyPolicy(Protocol):
    """Live grant policy required by Proposal lifecycle orchestration."""

    async def covers_all(self, categories: Sequence[ActionCategory]) -> bool:
        """Return whether every category is covered by a live grant."""
        ...

    async def revocable_grant_ids(
        self, categories: Sequence[ActionCategory]
    ) -> list[UUID7]:
        """Return live grants covering at least one category."""
        ...


class _Aggregate:
    """Mutable per-category calibration accumulator built from Proposal history."""

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
        """Fold one action and its owning proposal into the category counts."""
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

    def to_suggestion(self, category: ActionCategory) -> GrantSuggestion:
        """Render the accumulated counts as a calibration suggestion."""
        return GrantSuggestion(
            kind=category.kind,
            scope=category.scope,
            seen=len(self.seen),
            approved=len(self.approved),
            rejected=len(self.rejected),
            edited=len(self.edited),
            last_rejection=self.last_rejection,
        )


class ProposalAutonomyService:
    """Own the live autonomy grant ledger for Proposal action categories.

    >>> service = ProposalAutonomyService(database=database)
    >>> grant = await service.grant("gmail.archive", None, now=now)
    >>> await service.list_grants() == [grant]
    True
    """

    def __init__(
        self,
        database: Database,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def calibration_stats(self) -> list[GrantSuggestion]:
        """Compute grant suggestions from Proposal history at read time."""
        async with self.database.transaction() as transaction:
            proposals = await transaction.fetch_all(select(Proposal).all())
            actions = await transaction.fetch_all(select(ProposalAction).all())
        proposals_by_id = {str(proposal.id): proposal for proposal in proposals}
        edited_proposal_ids = {
            action.proposal_id
            for action in actions
            if action.disposition == "deselected"
        }
        aggregates: dict[ActionCategory, _Aggregate] = {}
        for action in actions:
            proposal = proposals_by_id.get(action.proposal_id)
            if proposal is None:
                continue
            category = ActionCategory(kind=action.kind, scope=action.scope)
            aggregates.setdefault(category, _Aggregate()).observe(
                action,
                proposal,
                edited=action.proposal_id in edited_proposal_ids,
            )
        grants = await self.list_grants()
        return [
            aggregate.to_suggestion(category)
            for category, aggregate in aggregates.items()
            if not any(self._grant_matches(grant, category) for grant in grants)
        ]

    async def grant(
        self, kind: str, scope: str | None, *, now: datetime
    ) -> AutonomyGrant[Fetched]:
        """Grant autonomy for a `(kind, scope)` category."""
        _ = now

        async def _grant(transaction: Transaction) -> AutonomyGrant[Fetched]:
            return await transaction.execute(
                insert(AutonomyGrant(kind=kind, scope=scope)).returning()
            )

        async with self.database.transaction(mode="immediate") as transaction:
            granted = await _grant(transaction)
        await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))
        return granted

    async def covers_all(self, categories: Sequence[ActionCategory]) -> bool:
        """Return whether every category is covered by a live grant."""
        grants = await self.list_grants()
        return all(
            any(self._grant_matches(grant, category) for grant in grants)
            for category in categories
        )

    async def revocable_grant_ids(
        self, categories: Sequence[ActionCategory]
    ) -> list[UUID7]:
        """Return live grants covering at least one supplied category."""
        grants = await self.list_grants()
        return sorted(
            {
                grant.id
                for category in categories
                for grant in grants
                if self._grant_matches(grant, category)
            }
        )

    async def revoke(self, grant_id: UUID7, *, now: datetime) -> None:
        """Revoke a grant convergently; absent or revoked ids are no-ops."""

        async def _revoke(transaction: Transaction) -> int:
            return await transaction.execute(
                update(AutonomyGrant)
                .set(AutonomyGrant.revoked_at.to(now))
                .where(AutonomyGrant.id.eq(grant_id))
                .where(AutonomyGrant.revoked_at.is_null())
            )

        async with self.database.transaction(mode="immediate") as transaction:
            matched = await _revoke(transaction)
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["proposals"]))

    async def list_grants(self) -> list[AutonomyGrant[Fetched]]:
        """List live grants, newest first."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(AutonomyGrant)
                .where(AutonomyGrant.revoked_at.is_null())
                .order_by(AutonomyGrant.granted_at.desc())
                .order_by(AutonomyGrant.id.desc())
            )

    @staticmethod
    def _grant_matches(grant: AutonomyGrant[Fetched], category: ActionCategory) -> bool:
        """Match exact kinds while a bare scope covers every action scope."""
        return grant.kind == category.kind and (
            grant.scope is None or grant.scope == category.scope
        )
