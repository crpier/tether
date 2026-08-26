"""Behavior tests for Proposal autonomy and calibration policy."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime

from snekql.sqlite import Config, Database
from snektest import assert_eq, fixture, load_fixture, test

from tether.events import HubEvent, InvalidateEvent
from tether.proposal_autonomy import ActionCategory, ProposalAutonomyService
from tether.proposal_store import create_proposal_schema

NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)


class RecordingPublisher:
    """Capture domain events published by autonomy mutations."""

    def __init__(self) -> None:
        self.events: list[HubEvent] = []

    async def publish(self, event: HubEvent) -> None:
        """Record one domain event."""
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class Harness:
    """An autonomy service over an isolated Proposal store."""

    publisher: RecordingPublisher
    service: ProposalAutonomyService


@fixture
async def harness() -> AsyncGenerator[Harness]:
    """Build an autonomy service over a fresh in-memory database."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_proposal_schema(database)
    publisher = RecordingPublisher()
    yield Harness(
        publisher=publisher,
        service=ProposalAutonomyService(
            database=database,
            event_publisher=publisher,
        ),
    )
    await database.close()


@test()
async def granted_category_is_listed_live() -> None:
    """A newly granted category appears in the live grant ledger."""
    autonomy = await load_fixture(harness())

    granted = await autonomy.service.grant("test.ok", "newsletter", now=NOW)

    live_grants = await autonomy.service.list_grants()
    assert_eq([grant.id for grant in live_grants], [granted.id])


@test()
async def grant_invalidates_proposal_views() -> None:
    """Granting autonomy invalidates proposal and calibration views."""
    autonomy = await load_fixture(harness())

    _ = await autonomy.service.grant("test.ok", None, now=NOW)

    assert_eq(autonomy.publisher.events, [InvalidateEvent(keys=["proposals"])])


@test()
async def revoke_convergently_removes_a_live_grant() -> None:
    """Repeated revocation leaves the grant absent from the live ledger."""
    autonomy = await load_fixture(harness())
    granted = await autonomy.service.grant("test.ok", None, now=NOW)

    await autonomy.service.revoke(granted.id, now=NOW)
    await autonomy.service.revoke(granted.id, now=NOW)

    assert_eq(await autonomy.service.list_grants(), [])
    assert_eq(
        autonomy.publisher.events,
        [
            InvalidateEvent(keys=["proposals"]),
            InvalidateEvent(keys=["proposals"]),
        ],
    )


@test()
async def scoped_grant_covers_only_the_same_scope() -> None:
    """A scoped grant fails closed for a different action scope."""
    autonomy = await load_fixture(harness())
    _ = await autonomy.service.grant("test.ok", "newsletter", now=NOW)

    matching = await autonomy.service.covers_all(
        [ActionCategory(kind="test.ok", scope="newsletter")]
    )
    different = await autonomy.service.covers_all(
        [ActionCategory(kind="test.ok", scope="receipt")]
    )

    assert_eq(matching, True)
    assert_eq(different, False)


@test()
async def bare_kind_grant_covers_every_scope() -> None:
    """A bare-kind grant covers scoped and unscoped actions of its kind."""
    autonomy = await load_fixture(harness())
    _ = await autonomy.service.grant("test.ok", None, now=NOW)

    covered = await autonomy.service.covers_all(
        [
            ActionCategory(kind="test.ok", scope=None),
            ActionCategory(kind="test.ok", scope="newsletter"),
        ]
    )

    assert_eq(covered, True)


@test()
async def covering_grants_are_returned_as_revocable() -> None:
    """Only live grants covering supplied categories are revocable candidates."""
    autonomy = await load_fixture(harness())
    bare = await autonomy.service.grant("test.ok", None, now=NOW)
    scoped = await autonomy.service.grant("test.ok", "newsletter", now=NOW)
    _ = await autonomy.service.grant("test.fail", None, now=NOW)

    grant_ids = await autonomy.service.revocable_grant_ids(
        [ActionCategory(kind="test.ok", scope="newsletter")]
    )

    assert_eq(grant_ids, sorted([bare.id, scoped.id]))


@test()
async def every_action_category_requires_coverage() -> None:
    """One uncovered category fails the entire Proposal coverage check."""
    autonomy = await load_fixture(harness())
    _ = await autonomy.service.grant("test.ok", None, now=NOW)

    covered = await autonomy.service.covers_all(
        [
            ActionCategory(kind="test.ok", scope=None),
            ActionCategory(kind="test.fail", scope=None),
        ]
    )

    assert_eq(covered, False)
