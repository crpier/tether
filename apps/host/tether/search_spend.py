"""Persisted monthly credit accounting for external Web Search."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from snekok.result import Err, Ok, Result
from snekql.sqlite import Database, DoUpdate, Transaction, insert, select

from tether.capability_contracts import QuotaMeta
from tether.web_search import SearchBudgetExhaustedFailure
from tether.youtube import SystemClock, YouTubeSyncState

_SPEND_KEY_PREFIX = "search_uses"
"""Prefix for persisted UTC-month Web Search credit counters."""


def _month_key(now: datetime) -> str:
    """The persisted spend key for `now`'s UTC calendar month."""
    return f"{_SPEND_KEY_PREFIX}:{now.astimezone(UTC):%Y-%m}"


class SearchSpendGuard(Protocol):
    """Reserve provider credits before a Web Search request is sent."""

    async def charge(
        self, credit_cost: int
    ) -> Result[None, SearchBudgetExhaustedFailure]:
        """Reserve credits, returning exhaustion instead of sending upstream."""
        ...

    async def snapshot(self) -> QuotaMeta | None:
        """Report current usage, or `None` when uncapped."""
        ...


class UnlimitedSearchSpend:
    """An uncapped spend guard for explicitly injected providers."""

    async def charge(
        self, credit_cost: int
    ) -> Result[None, SearchBudgetExhaustedFailure]:
        """Allow spending without a cap."""
        _ = credit_cost
        return Ok(None)

    async def snapshot(self) -> QuotaMeta | None:
        """Return no quota metadata because spending is uncapped."""
        return None


class PersistentSearchSpendGuard:
    """A hard UTC-calendar-month Web Search credit cap persisted in SQLite."""

    def __init__(
        self, database: Database, *, max_uses: int, clock: SystemClock | None = None
    ) -> None:
        self._clock: SystemClock = clock or SystemClock()
        self._database: Database = database
        self._max_uses: int = max(0, max_uses)

    async def charge(
        self, credit_cost: int
    ) -> Result[None, SearchBudgetExhaustedFailure]:
        """Reserve a call's credits, returning one that would cross the cap."""
        month_key = _month_key(self._clock.now())

        async def _reserve(
            transaction: Transaction,
        ) -> SearchBudgetExhaustedFailure | None:
            row = await transaction.fetch_one_or_none(
                select(YouTubeSyncState).where(YouTubeSyncState.key.eq(month_key))
            )
            used = int(row.value) if row is not None else 0
            if used + credit_cost > self._max_uses:
                return SearchBudgetExhaustedFailure(limit=self._max_uses, used=used)
            _ = await transaction.execute(
                insert(
                    YouTubeSyncState(
                        key=month_key,
                        value=str(used + credit_cost),
                    )
                ).on_conflict(
                    YouTubeSyncState.key,
                    action=DoUpdate(YouTubeSyncState.value.to_inserted()),
                )
            )
            return None

        async with self._database.transaction(mode="immediate") as transaction:
            failure = await _reserve(transaction)
        if failure is not None:
            return Err(failure)
        return Ok(None)

    async def snapshot(self) -> QuotaMeta:
        """Read the current month's credit use without reserving more."""
        async with self._database.transaction() as transaction:
            row = await transaction.fetch_one_or_none(
                select(YouTubeSyncState).where(
                    YouTubeSyncState.key.eq(_month_key(self._clock.now()))
                )
            )
        used = int(row.value) if row is not None else 0
        return QuotaMeta(
            limit=self._max_uses,
            remaining=max(0, self._max_uses - used),
            used=used,
        )
