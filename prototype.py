"""PROTOTYPE: API clients own enforcement but receive budget allocation.

Run from the repository root:

    uv run --project apps/host python prototype.py

This is deliberately standalone and in-memory. It explores typing and API shape,
not production persistence, calendar windows, concurrency, or refunds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from snekok import Err, Ok, Result

# A standalone executable intentionally lives outside a Python package.
# ruff: noqa: INP001

_MIN_TRANSCRIPT_LENGTH = 10


@dataclass(frozen=True, slots=True)
class BudgetAllocation:
    """Application policy allocating a maximum number of units to one API client."""

    limit: int


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """The current state of one API client's usage budget."""

    limit: int
    remaining: int
    used: int


@dataclass(frozen=True, slots=True)
class BudgetExhausted:
    """A reservation that would exceed an API client's allocation."""

    limit: int
    requested: int
    used: int


@dataclass(frozen=True, slots=True)
class Budgeted[**Parameters, ValueT, ErrorT]:
    """A callable whose operation already includes budget reservation.

    Transformations compose after protection. Calling the final pipeline reserves
    exactly once regardless of how many transformations were added.
    """

    _run: Callable[Parameters, Awaitable[Result[ValueT, ErrorT]]]

    async def __call__(
        self, *args: Parameters.args, **kwargs: Parameters.kwargs
    ) -> Result[ValueT, ErrorT]:
        """Invoke the operation through its inseparable reservation."""
        return await self._run(*args, **kwargs)

    def map[MappedT](
        self, transform: Callable[[ValueT], MappedT]
    ) -> Budgeted[Parameters, MappedT, ErrorT]:
        """Transform a protected operation's success value."""

        async def mapped(
            *args: Parameters.args, **kwargs: Parameters.kwargs
        ) -> Result[MappedT, ErrorT]:
            return (await self(*args, **kwargs)).map(transform)

        return Budgeted(mapped)

    def map_error[MappedErrorT](
        self, transform: Callable[[ErrorT], MappedErrorT]
    ) -> Budgeted[Parameters, ValueT, MappedErrorT]:
        """Translate every failure exposed by a protected operation."""

        async def mapped_error(
            *args: Parameters.args, **kwargs: Parameters.kwargs
        ) -> Result[ValueT, MappedErrorT]:
            return (await self(*args, **kwargs)).map_error(transform)

        return Budgeted(mapped_error)

    def and_then[MappedT, AddedErrorT](
        self,
        transform: Callable[[ValueT], Result[MappedT, AddedErrorT]],
    ) -> Budgeted[Parameters, MappedT, ErrorT | AddedErrorT]:
        """Continue with synchronous fallible work after the protected operation."""

        async def continued(
            *args: Parameters.args, **kwargs: Parameters.kwargs
        ) -> Result[MappedT, ErrorT | AddedErrorT]:
            return (await self(*args, **kwargs)).and_then(transform)

        return Budgeted(continued)


class InMemoryBudgetLedger:
    """One API client's private allocation state and reservation interpreter."""

    def __init__(self, allocation: BudgetAllocation) -> None:
        self._limit: int = allocation.limit
        self._used: int = 0

    async def reserve(self, *, units: int) -> Result[BudgetSnapshot, BudgetExhausted]:
        """Reserve units or return the unchanged exhausted state."""
        if self._used + units > self._limit:
            return Err(
                BudgetExhausted(
                    limit=self._limit,
                    requested=units,
                    used=self._used,
                )
            )
        self._used += units
        return Ok(self.snapshot())

    def snapshot(self) -> BudgetSnapshot:
        """Expose this client's budget state without reserving."""
        return BudgetSnapshot(
            limit=self._limit,
            remaining=self._limit - self._used,
            used=self._used,
        )


def within_budget[**Parameters, ValueT, ErrorT](
    operation: Callable[Parameters, Awaitable[Result[ValueT, ErrorT]]],
    *,
    ledger: InMemoryBudgetLedger,
    units: int,
) -> Budgeted[Parameters, ValueT, ErrorT | BudgetExhausted]:
    """Protect one spending operation with its owning client's private ledger."""

    async def protected(
        *args: Parameters.args, **kwargs: Parameters.kwargs
    ) -> Result[ValueT, ErrorT | BudgetExhausted]:
        match await ledger.reserve(units=units):
            case Err(exhausted):
                return Err(exhausted)
            case Ok(_snapshot):
                return await operation(*args, **kwargs)

    return Budgeted(protected)


@dataclass(frozen=True, slots=True)
class TranscriptUpstreamFailure:
    """The fake transcript upstream rejected an admitted request."""

    reason: str


@dataclass(frozen=True, slots=True)
class TranscriptTooShort:
    """A fetched transcript failed a local client rule."""

    length: int


@dataclass(frozen=True, slots=True)
class TranscriptFailure:
    """The failure vocabulary exposed by the transcript API client."""

    message: str


def _require_substantial_transcript(
    transcript: str,
) -> Result[str, TranscriptTooShort]:
    """Apply a fallible local rule after an admitted upstream request."""
    if len(transcript) < _MIN_TRANSCRIPT_LENGTH:
        return Err(TranscriptTooShort(length=len(transcript)))
    return Ok(transcript)


def _present_transcript_failure(
    failure: BudgetExhausted | TranscriptTooShort | TranscriptUpstreamFailure,
) -> TranscriptFailure:
    """Translate allocation, upstream, and domain failures at the client boundary."""
    match failure:
        case BudgetExhausted(limit=limit, requested=requested, used=used):
            message = (
                f"transcript budget exhausted: {used}/{limit} used, "
                f"{requested} requested"
            )
        case TranscriptTooShort(length=length):
            message = f"transcript too short: {length} characters"
        case TranscriptUpstreamFailure(reason=reason):
            message = f"transcript upstream failed: {reason}"
    return TranscriptFailure(message=message)


class TranscriptApi:
    """A fake API client that owns enforcement around its spending operation.

    Application wiring chooses the allocation. This client chooses the cost of a
    fetch because it owns the upstream operation and knows how that operation is
    billed. Only the protected callable is retained after construction.
    """

    _FETCH_COST: ClassVar[int] = 1

    def __init__(self, *, allocation: BudgetAllocation) -> None:
        self._ledger: InMemoryBudgetLedger = InMemoryBudgetLedger(allocation)
        self._fetch: Budgeted[[str], str, TranscriptFailure] = (
            within_budget(
                self._request_upstream,
                ledger=self._ledger,
                units=self._FETCH_COST,
            )
            .map(str.upper)
            .and_then(_require_substantial_transcript)
            .map_error(_present_transcript_failure)
        )
        self.upstream_calls: int = 0

    async def fetch(self, video_id: str) -> Result[str, TranscriptFailure]:
        """Fetch through this client's inseparable private budget."""
        return await self._fetch(video_id)

    def budget_snapshot(self) -> BudgetSnapshot:
        """Report this client's independent allocation state."""
        return self._ledger.snapshot()

    async def _request_upstream(
        self, video_id: str
    ) -> Result[str, TranscriptUpstreamFailure]:
        """Stand in for the network operation guarded by this client."""
        self.upstream_calls += 1
        return Ok(f"transcript for {video_id}")


class ResearchAssistant:
    """A fake consumer unconcerned with transcript billing or enforcement."""

    def __init__(self, transcript_api: TranscriptApi) -> None:
        self._transcript_api: TranscriptApi = transcript_api

    async def research(self, video_id: str) -> Result[str, TranscriptFailure]:
        """Use an ordinary client method with no visible budget operation."""
        return await self._transcript_api.fetch(video_id)


@dataclass(frozen=True, slots=True)
class MailUpstreamFailure:
    """The fake mail upstream rejected an admitted delivery."""

    reason: str


class MailService:
    """A second client with its own allocation and operation cost."""

    _DELIVERY_COST: ClassVar[int] = 1

    def __init__(self, *, allocation: BudgetAllocation) -> None:
        self._ledger: InMemoryBudgetLedger = InMemoryBudgetLedger(allocation)
        self._deliver: Budgeted[
            [str, str], str, BudgetExhausted | MailUpstreamFailure
        ] = within_budget(
            self._deliver_upstream,
            ledger=self._ledger,
            units=self._DELIVERY_COST,
        )
        self.upstream_calls: int = 0

    async def send(
        self, address: str, body: str
    ) -> Result[str, BudgetExhausted | MailUpstreamFailure]:
        """Deliver through this client's inseparable private budget."""
        return await self._deliver(address, body)

    def budget_snapshot(self) -> BudgetSnapshot:
        """Report this client's independent allocation state."""
        return self._ledger.snapshot()

    async def _deliver_upstream(
        self, address: str, body: str
    ) -> Result[str, MailUpstreamFailure]:
        """Stand in for the network operation guarded by this client."""
        self.upstream_calls += 1
        return Ok(f"receipt-{self.upstream_calls}:{address}:{body}")


class SignupFlow:
    """A fake consumer unconcerned with mail billing or enforcement."""

    def __init__(self, mail_service: MailService) -> None:
        self._mail_service: MailService = mail_service

    async def welcome(
        self, address: str
    ) -> Result[str, BudgetExhausted | MailUpstreamFailure]:
        """Use an ordinary service method with no visible budget operation."""
        return await self._mail_service.send(address, "Welcome!")


@dataclass(frozen=True, slots=True)
class Demo:
    """Both independently allocated clients displayed after each action."""

    mail_service: MailService
    transcript_api: TranscriptApi


def _show_state(label: str, outcome: object, *, demo: Demo) -> None:
    """Print each outcome and both clients' independent state."""
    print(f"\n{label}")
    print(f"  outcome: {outcome!r}")
    print(f"  transcript budget: {demo.transcript_api.budget_snapshot()!r}")
    print(f"  mail budget: {demo.mail_service.budget_snapshot()!r}")
    print(
        f"  upstream calls: transcript={demo.transcript_api.upstream_calls},",
        f"email={demo.mail_service.upstream_calls}",
    )


async def main() -> None:
    """Demonstrate external allocation and client-owned enforcement."""
    transcript_api = TranscriptApi(allocation=BudgetAllocation(limit=2))
    mail_service = MailService(allocation=BudgetAllocation(limit=1))
    researcher = ResearchAssistant(transcript_api)
    signup = SignupFlow(mail_service)
    demo = Demo(mail_service=mail_service, transcript_api=transcript_api)

    for video_id in ("video-1", "video-2", "video-3"):
        _show_state(
            f"research {video_id}",
            await researcher.research(video_id),
            demo=demo,
        )

    for address in ("first@example.test", "second@example.test"):
        _show_state(
            f"welcome {address}",
            await signup.welcome(address),
            demo=demo,
        )


if __name__ == "__main__":
    asyncio.run(main())
