"""Behaviour tests for the Proposal service layer.

These drive the *service* seam directly against a real in-memory SQLite database
— no HTTP, no agent — with a fake action registry (kinds `test.ok`, `test.fail`,
`test.skip`) so the lifecycle, grant matching, executor loop, crash-resume, and
calibration statistics are exercised without any real consumer.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel
from snekql.sqlite import Config, Database, update
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.action_registry import (
    ActionContext,
    ActionResult,
    ActionSpec,
    build_action_registry,
)
from tether.events import HubEvent, InvalidateEvent
from tether.logging import Logger
from tether.notifications import NotificationService, create_notification_schema
from tether.proposals import (
    ActionDraft,
    InvalidActionError,
    Proposal,
    ProposalAction,
    ProposalConflictError,
    ProposalDraft,
    ProposalService,
    create_proposal_schema,
)

LOGGER: Logger = structlog.stdlib.get_logger("test.proposals_service")
NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)


class NoParams(BaseModel):
    """A params model that accepts an empty object, for fake action kinds."""


class RequiredParams(BaseModel):
    """A params model requiring a field, to exercise propose-time validation."""

    target: str


@dataclass
class ExecutorCalls:
    """Records how many times each fake executor ran, for idempotency checks."""

    ok: int = 0
    fail: int = 0
    skip: int = 0


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.proposals_service")


class RecordingPublisher:
    """Captures every event a service publishes, for assertion in tests."""

    def __init__(self) -> None:
        self.events: list[HubEvent] = []

    async def publish(self, event: HubEvent) -> None:
        """Record a published event."""
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class Harness:
    """The wired proposal service plus its collaborators for assertions."""

    service: ProposalService
    publisher: RecordingPublisher
    notifications: NotificationService
    calls: ExecutorCalls


def _registry(calls: ExecutorCalls) -> dict[str, ActionSpec]:
    """Build a fake action registry whose executors record their calls."""

    async def ok(params: BaseModel, context: ActionContext) -> ActionResult:
        _ = params, context
        calls.ok += 1
        return ActionResult(outcome="succeeded", detail="did it")

    async def fail(params: BaseModel, context: ActionContext) -> ActionResult:
        _ = params, context
        calls.fail += 1
        return ActionResult(outcome="failed", detail="nope")

    async def skip(params: BaseModel, context: ActionContext) -> ActionResult:
        _ = params, context
        calls.skip += 1
        return ActionResult(outcome="skipped", detail="already done")

    return build_action_registry(
        [
            ActionSpec("test.ok", NoParams, ok, ui_hint="test.ok"),
            ActionSpec("test.fail", NoParams, fail, ui_hint="test.fail"),
            ActionSpec("test.skip", NoParams, skip, ui_hint="test.skip"),
            ActionSpec("test.required", RequiredParams, ok, ui_hint="test.required"),
        ]
    )


@fixture
async def harness() -> AsyncGenerator[Harness]:
    """A proposal service wired to a fake registry, notifications, and publisher."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_proposal_schema(db)
    await create_notification_schema(db)
    publisher = RecordingPublisher()
    notifications = NotificationService(database=db)
    calls = ExecutorCalls()
    service = ProposalService(
        db,
        noop_tracer(),
        event_publisher=publisher,
        action_registry=_registry(calls),
        notification_service=notifications,
    )
    yield Harness(
        service=service, publisher=publisher, notifications=notifications, calls=calls
    )
    await db.close()


def draft(*actions: ActionDraft, title: str = "Do things") -> ProposalDraft:
    """A proposal draft over the given actions."""
    return ProposalDraft(
        consumer="test", title=title, summary="a summary", actions=list(actions)
    )


def action(kind: str, scope: str | None = None) -> ActionDraft:
    """A fake action draft with empty params."""
    return ActionDraft(kind=kind, scope=scope, params={})


# --- validation ------------------------------------------------------------


@test()
async def create_rejects_an_unknown_kind() -> None:
    """An action naming an unregistered kind is an InvalidActionError."""
    h = await load_fixture(harness())

    with assert_raises(InvalidActionError):
        _ = await h.service.create(draft(action("test.nope")), now=NOW, logger=LOGGER)


@test()
async def create_rejects_bad_params() -> None:
    """Params failing the kind's model are an InvalidActionError, not a stored row."""
    h = await load_fixture(harness())

    with assert_raises(InvalidActionError):
        _ = await h.service.create(
            draft(ActionDraft(kind="test.required", scope=None, params={})),
            now=NOW,
            logger=LOGGER,
        )


# --- queue vs auto-execute -------------------------------------------------


@test()
async def uncovered_proposal_queues_and_notifies() -> None:
    """With no grant, a proposal stays pending, notifies, and does not execute."""
    h = await load_fixture(harness())

    creation = await h.service.create(
        draft(action("test.ok"), title="Queue me"), now=NOW, logger=LOGGER
    )

    assert_eq(creation.auto_executed, False)
    assert_eq(creation.proposal.proposal.state, "pending")
    assert_eq(h.calls.ok, 0)
    recorded = await h.notifications.list_recent()
    assert_eq(len(recorded), 1)
    assert_eq(recorded[0].source_label, "Queue me")
    assert_in_events(h.publisher, InvalidateEvent(keys=["proposals"]))


@test()
async def fully_covered_proposal_auto_executes() -> None:
    """When every action is grant-covered the host runs the batch immediately."""
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", None, now=NOW)

    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)

    assert_eq(creation.auto_executed, True)
    assert_eq(creation.proposal.proposal.state, "executed")
    assert_eq(h.calls.ok, 1)
    assert_eq(creation.proposal.actions[0].outcome, "succeeded")
    # An auto-executed proposal does not queue as pending.
    assert_eq(await h.notifications.list_recent(), [])


@test()
async def bare_kind_grant_covers_all_scopes() -> None:
    """A bare-kind grant covers an action carrying any scope."""
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", None, now=NOW)

    creation = await h.service.create(
        draft(action("test.ok", scope="anything")), now=NOW, logger=LOGGER
    )

    assert_eq(creation.auto_executed, True)


@test()
async def typo_scope_fails_closed_and_queues() -> None:
    """A scoped grant does not match a differently-scoped action; it queues."""
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", "newsletter", now=NOW)

    creation = await h.service.create(
        draft(action("test.ok", scope="newsletterr")), now=NOW, logger=LOGGER
    )

    assert_eq(creation.auto_executed, False)
    assert_eq(creation.proposal.proposal.state, "pending")


@test()
async def any_uncovered_action_queues_the_whole_proposal() -> None:
    """One uncovered action queues the entire batch — never a partial run."""
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", None, now=NOW)

    creation = await h.service.create(
        draft(action("test.ok"), action("test.fail")), now=NOW, logger=LOGGER
    )

    assert_eq(creation.auto_executed, False)
    assert_eq(h.calls.ok, 0)
    assert_eq(h.calls.fail, 0)


# --- lifecycle: approve / execute / reject ---------------------------------


@test()
async def approve_executes_to_executed() -> None:
    """Approving a pending proposal runs it through to executed."""
    h = await load_fixture(harness())
    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    ref = creation.proposal.proposal

    view = await h.service.approve(
        ref, deselected_action_ids=set(), now=NOW, logger=LOGGER
    )

    assert_eq(view.proposal.state, "executed")
    assert_is_not_none(view.proposal.decided_at)
    assert_eq(view.actions[0].outcome, "succeeded")
    assert_eq(h.calls.ok, 1)


@test()
async def a_failed_action_lands_the_proposal_in_failed() -> None:
    """Any approved action failing settles the proposal as failed."""
    h = await load_fixture(harness())
    creation = await h.service.create(
        draft(action("test.ok"), action("test.fail")), now=NOW, logger=LOGGER
    )

    view = await h.service.approve(
        creation.proposal.proposal,
        deselected_action_ids=set(),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(view.proposal.state, "failed")
    assert_eq(view.actions[0].outcome, "succeeded")
    assert_eq(view.actions[1].outcome, "failed")


@test()
async def deselected_actions_are_not_executed() -> None:
    """A deselected action is recorded deselected and never runs."""
    h = await load_fixture(harness())
    creation = await h.service.create(
        draft(action("test.ok"), action("test.fail")), now=NOW, logger=LOGGER
    )
    fail_action = creation.proposal.actions[1]

    view = await h.service.approve(
        creation.proposal.proposal,
        deselected_action_ids={fail_action.id},
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(view.proposal.state, "executed")
    assert_eq(view.actions[1].disposition, "deselected")
    assert_is_none(view.actions[1].outcome)
    assert_eq(h.calls.fail, 0)


@test()
async def a_skip_outcome_is_not_an_error() -> None:
    """A stale target resolves skipped, and the proposal still reaches executed."""
    h = await load_fixture(harness())
    creation = await h.service.create(
        draft(action("test.skip")), now=NOW, logger=LOGGER
    )

    view = await h.service.approve(
        creation.proposal.proposal,
        deselected_action_ids=set(),
        now=NOW,
        logger=LOGGER,
    )

    assert_eq(view.proposal.state, "executed")
    assert_eq(view.actions[0].outcome, "skipped")


@test()
async def approve_with_a_stale_version_conflicts() -> None:
    """Approving at an out-of-date version is a conflict."""
    h = await load_fixture(harness())
    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    ref = creation.proposal.proposal
    _ = await h.service.approve(
        ref, deselected_action_ids=set(), now=NOW, logger=LOGGER
    )

    with assert_raises(ProposalConflictError):
        _ = await h.service.approve(
            ref, deselected_action_ids=set(), now=NOW, logger=LOGGER
        )


@test()
async def reject_is_terminal_with_a_reason() -> None:
    """Rejecting a pending proposal is terminal and records the reason."""
    h = await load_fixture(harness())
    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)

    outcome = await h.service.reject(
        creation.proposal.proposal, reason="not now", now=NOW, logger=LOGGER
    )

    assert_eq(outcome.proposal.proposal.state, "rejected")
    assert_eq(outcome.proposal.proposal.rejection_reason, "not now")
    assert_eq(outcome.revocable_grant_ids, [])


@test()
async def reject_in_a_granted_category_signals_revocation() -> None:
    """Rejecting an action in a granted category returns the covering grant id."""
    h = await load_fixture(harness())
    granted = await h.service.grant("test.fail", None, now=NOW)
    # Mix a covered and an uncovered action so the proposal still queues.
    creation = await h.service.create(
        draft(action("test.ok"), action("test.fail")), now=NOW, logger=LOGGER
    )

    outcome = await h.service.reject(
        creation.proposal.proposal, reason=None, now=NOW, logger=LOGGER
    )

    assert_eq(outcome.revocable_grant_ids, [granted.id])


@test()
async def reject_with_a_stale_version_conflicts() -> None:
    """Rejecting at an out-of-date version is a conflict."""
    h = await load_fixture(harness())
    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    ref = creation.proposal.proposal
    _ = await h.service.reject(ref, reason=None, now=NOW, logger=LOGGER)

    with assert_raises(ProposalConflictError):
        _ = await h.service.reject(ref, reason=None, now=NOW, logger=LOGGER)


# --- executor idempotency / crash-resume -----------------------------------


@test()
async def execute_skips_already_resolved_actions_on_rerun() -> None:
    """Re-running an interrupted batch skips done actions and finishes the rest.

    Simulates a crash mid-`executing`: the proposal is forced back to executing
    with its first action already succeeded and its second still NULL. A fresh
    `execute` must run only the NULL action and leave the done one untouched.
    """
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", None, now=NOW)
    creation = await h.service.create(
        draft(action("test.ok"), action("test.ok")), now=NOW, logger=LOGGER
    )
    proposal_id = creation.proposal.proposal.id
    assert_eq(h.calls.ok, 2)

    # Force a crash-resume shape: back to executing, second action's outcome NULL.
    second = creation.proposal.actions[1]
    await _force_executing(h, proposal_id)
    await _clear_outcome(h, second.id)

    view = await h.service.execute(proposal_id, now=NOW, logger=LOGGER)

    # Only the cleared action re-ran (3 total); the first stayed succeeded.
    assert_eq(h.calls.ok, 3)
    assert_eq(view.proposal.state, "executed")
    assert_eq([a.outcome for a in view.actions], ["succeeded", "succeeded"])


@test()
async def outcomes_are_append_only_across_reruns() -> None:
    """A terminal outcome is never overwritten by a second execute pass."""
    h = await load_fixture(harness())
    _ = await h.service.grant("test.ok", None, now=NOW)
    creation = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    proposal_id = creation.proposal.proposal.id

    await _force_executing(h, proposal_id)
    view = await h.service.execute(proposal_id, now=NOW, logger=LOGGER)

    # The already-succeeded action was not re-run (still one call).
    assert_eq(h.calls.ok, 1)
    assert_eq(view.actions[0].outcome, "succeeded")


# --- grants + calibration --------------------------------------------------


@test()
async def revoke_is_convergent() -> None:
    """Revoking removes a grant from the live list; re-revoking is a no-op."""
    h = await load_fixture(harness())
    granted = await h.service.grant("test.ok", None, now=NOW)

    await h.service.revoke(granted.id, now=NOW)
    await h.service.revoke(granted.id, now=NOW)

    assert_eq(await h.service.list_grants(), [])


@test()
async def calibration_counts_history_and_hides_granted() -> None:
    """Calibration aggregates history and omits currently-granted categories."""
    h = await load_fixture(harness())
    later = NOW + timedelta(hours=1)
    # A rejected proposal for test.ok.
    rejected = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    _ = await h.service.reject(
        rejected.proposal.proposal, reason="no", now=later, logger=LOGGER
    )
    # An approved (executed) proposal for test.ok, deselecting nothing.
    approved = await h.service.create(draft(action("test.ok")), now=NOW, logger=LOGGER)
    _ = await h.service.approve(
        approved.proposal.proposal,
        deselected_action_ids=set(),
        now=NOW,
        logger=LOGGER,
    )
    # A separate category that is granted — must not surface as a suggestion.
    _ = await h.service.grant("test.skip", None, now=NOW)
    granted_proposal = await h.service.create(
        draft(action("test.skip")), now=NOW, logger=LOGGER
    )
    _ = granted_proposal

    suggestions = await h.service.calibration_stats()

    by_kind = {s.kind: s for s in suggestions}
    assert_eq(set(by_kind), {"test.ok"})
    ok = by_kind["test.ok"]
    assert_eq(ok.seen, 2)
    assert_eq(ok.approved, 1)
    assert_eq(ok.rejected, 1)
    assert_is_not_none(ok.last_rejection)


@test()
async def calibration_counts_edited_proposals() -> None:
    """A proposal with any deselected action counts as edited for its category."""
    h = await load_fixture(harness())
    creation = await h.service.create(
        draft(action("test.ok"), action("test.fail")), now=NOW, logger=LOGGER
    )
    _ = await h.service.approve(
        creation.proposal.proposal,
        deselected_action_ids={creation.proposal.actions[1].id},
        now=NOW,
        logger=LOGGER,
    )

    suggestions = await h.service.calibration_stats()

    ok = next(s for s in suggestions if s.kind == "test.ok")
    assert_eq(ok.edited, 1)


# --- helpers ---------------------------------------------------------------


def assert_in_events(publisher: RecordingPublisher, event: HubEvent) -> None:
    """Assert a publisher emitted the given event at least once."""
    assert_eq(event in publisher.events, True)


async def _force_executing(h: Harness, proposal_id: UUID7) -> None:
    """Force a proposal back to the executing state, mimicking a mid-batch crash."""
    async with h.service.database.transaction() as tx:
        _ = await tx.execute(
            update(Proposal)
            .set(Proposal.state.to("executing"))
            .where(Proposal.id.eq(proposal_id))
        )


async def _clear_outcome(h: Harness, action_id: UUID7) -> None:
    """Clear one action's outcome so a re-run treats it as unprocessed."""
    async with h.service.database.transaction() as tx:
        _ = await tx.execute(
            update(ProposalAction)
            .set(ProposalAction.outcome.to(None))
            .set(ProposalAction.executed_at.to(None))
            .where(ProposalAction.id.eq(action_id))
        )
