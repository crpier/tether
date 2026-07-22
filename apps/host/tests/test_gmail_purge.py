"""Behaviour tests for the Gmail backlog-purge sweep.

Drive `GmailPurgeSweepService` against a real in-memory `ProposalService` and a
scripted Gmail transport + triage runner — no live Gmail, no agent. They assert
the sweep chunks the backlog and composes one Proposal per chunk (consumer
`gmail`, sender-category scopes set), that a bad per-message verdict is dropped
from the proposal, that the sweep performs NO direct mailbox writes (only
proposes), and that its own watermark (a separate sync-state key) resumes across
passes so a second pass queries incrementally.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, select
from snektest import (
    assert_eq,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.gmail import (
    GmailClient,
    GmailResponse,
    GmailSyncState,
    create_gmail_schema,
)
from tether.gmail_purge import GmailPurgeSweepService
from tether.logging import Logger
from tether.proposals import ProposalService, create_proposal_schema


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.gmail_purge")


def test_logger() -> Logger:
    """A throwaway structured logger."""
    return structlog.stdlib.get_logger("test.gmail_purge")


# --- Scripted transport + triage runner -------------------------------------


@dataclass
class PurgeTransport:
    """A read `GmailTransport` for the sweep that records any (illegal) writes."""

    message_pages: list[dict[str, object]]
    messages: dict[str, dict[str, object]]
    labels: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    list_calls: list[str] = field(default_factory=list[str])
    modify_calls: list[str] = field(default_factory=list[str])
    trash_calls: list[str] = field(default_factory=list[str])

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        _ = page_token
        self.list_calls.append(query)
        return GmailResponse(status_code=200, payload=self.message_pages.pop(0))

    async def get_message(self, message_id: str) -> GmailResponse:
        payload = self.messages.get(message_id)
        if payload is None:
            return GmailResponse(status_code=404, payload={})
        return GmailResponse(status_code=200, payload=payload)

    async def list_labels(self) -> GmailResponse:
        return GmailResponse(status_code=200, payload={"labels": self.labels})

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> GmailResponse:
        _ = add_label_ids, remove_label_ids
        self.modify_calls.append(message_id)
        return GmailResponse(status_code=200, payload={})

    async def trash_message(self, message_id: str) -> GmailResponse:
        self.trash_calls.append(message_id)
        return GmailResponse(status_code=200, payload={})


@dataclass
class StubRunner:
    """A scripted `GmailTriageRunner`: replies handed out in order."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list[str])

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _message(
    message_id: str, *, from_header: str = "sender@example.com", subject: str = "Hi"
) -> dict[str, object]:
    """A minimal `messages.get` payload for a backlog inbox message."""
    return {
        "id": message_id,
        "threadId": message_id,
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {},
        },
    }


def _list_page(
    message_ids: Sequence[str], *, next_page_token: str | None = None
) -> dict[str, object]:
    return {
        "messages": [{"id": message_id} for message_id in message_ids],
        "nextPageToken": next_page_token,
    }


def purge_reply(entries: Sequence[dict[str, object]]) -> str:
    return json.dumps(list(entries))


def archive_verdict(
    message_id: str, *, category: str = "newsletter"
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "action": "archive",
        "sender_category": category,
    }


def label_verdict(
    message_id: str, *, label_name: str, category: str = "receipts"
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "action": "label",
        "sender_category": category,
        "label_name": label_name,
    }


def delete_verdict(message_id: str, *, category: str = "junk") -> dict[str, object]:
    return {"message_id": message_id, "action": "delete", "sender_category": category}


def keep_verdict(message_id: str) -> dict[str, object]:
    return {
        "message_id": message_id,
        "action": "keep",
        "sender_category": "important",
    }


# --- Fixture ----------------------------------------------------------------


@dataclass
class PurgeEnv:
    """A proposal-ready database plus a live `ProposalService`."""

    database: Database
    proposal_service: ProposalService
    logger: Logger

    def sweep_service(
        self,
        transport: PurgeTransport,
        runner: StubRunner,
        *,
        chunk_size: int = 10,
    ) -> GmailPurgeSweepService:
        return GmailPurgeSweepService(
            database=self.database,
            client=GmailClient(transport=transport),
            proposal_service=self.proposal_service,
            triage_runner=runner,
            chunk_size=chunk_size,
        )


@fixture
async def purge_env() -> AsyncGenerator[PurgeEnv]:
    """A fresh database with the proposal schema and a live `ProposalService`."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_proposal_schema(db)
    await create_gmail_schema(db)
    yield PurgeEnv(
        database=db,
        proposal_service=ProposalService(database=db, tracer=noop_tracer()),
        logger=test_logger(),
    )
    await db.close()


# --- One proposal per chunk -------------------------------------------------


@test()
async def a_chunk_of_actionable_verdicts_becomes_one_proposal() -> None:
    """A chunk with several actions folds into ONE gmail proposal, scopes set."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1", "m2"])],
        messages={"m1": _message("m1"), "m2": _message("m2")},
        labels=[{"id": "Label_7", "name": "receipts"}],
    )
    runner = StubRunner(
        replies=[
            purge_reply(
                [
                    archive_verdict("m1", category="newsletter"),
                    label_verdict("m2", label_name="receipts", category="receipts"),
                ]
            )
        ]
    )

    report = await env.sweep_service(transport, runner, chunk_size=10).sweep(
        logger=env.logger
    )

    assert_eq(report.scanned, 2)
    assert_eq(report.proposed, 1)
    assert_eq(report.actions, 2)
    proposals = await env.proposal_service.list_proposals(logger=env.logger)
    assert_eq(len(proposals), 1)
    view = proposals[0]
    assert_eq(view.proposal.consumer, "gmail")
    assert_eq(view.proposal.state, "pending")
    kinds = [action.kind for action in view.actions]
    assert_eq(kinds, ["gmail.archive", "gmail.label"])
    scopes = [action.scope for action in view.actions]
    assert_eq(scopes, ["sender-category:newsletter", "sender-category:receipts"])
    label_params = json.loads(view.actions[1].params_json)
    assert_eq(label_params["label_name"], "receipts")


@test()
async def the_sweep_chunks_the_backlog_into_separate_proposals() -> None:
    """A chunk size below the backlog size yields one proposal per chunk."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1", "m2"])],
        messages={"m1": _message("m1"), "m2": _message("m2")},
    )
    runner = StubRunner(
        replies=[
            purge_reply([archive_verdict("m1")]),
            purge_reply([delete_verdict("m2")]),
        ]
    )

    report = await env.sweep_service(transport, runner, chunk_size=1).sweep(
        logger=env.logger
    )

    assert_eq(report.proposed, 2)
    assert_eq(len(runner.prompts), 2)
    proposals = await env.proposal_service.list_proposals(logger=env.logger)
    assert_eq(len(proposals), 2)


@test()
async def a_keep_only_chunk_produces_no_proposal() -> None:
    """A chunk the model wants left alone composes no proposal."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1"])], messages={"m1": _message("m1")}
    )
    runner = StubRunner(replies=[purge_reply([keep_verdict("m1")])])

    report = await env.sweep_service(transport, runner).sweep(logger=env.logger)

    assert_eq(report.proposed, 0)
    assert_eq(await env.proposal_service.list_proposals(logger=env.logger), [])


# --- Bad verdict handling ---------------------------------------------------


@test()
async def a_bad_verdict_is_dropped_from_the_proposal() -> None:
    """A malformed per-message verdict is excluded; the rest still propose."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1", "m2"])],
        messages={"m1": _message("m1"), "m2": _message("m2")},
    )
    runner = StubRunner(
        replies=[
            purge_reply(
                [
                    archive_verdict("m1"),
                    {"message_id": "m2", "action": "bogus", "sender_category": "x"},
                ]
            )
        ]
    )

    report = await env.sweep_service(transport, runner).sweep(logger=env.logger)

    assert_eq(report.actions, 1)
    proposals = await env.proposal_service.list_proposals(logger=env.logger)
    assert_eq(len(proposals), 1)
    assert_eq([action.kind for action in proposals[0].actions], ["gmail.archive"])


@test()
async def a_label_verdict_without_a_name_is_dropped() -> None:
    """A `label` action missing its `label_name` is dropped defensively."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1"])], messages={"m1": _message("m1")}
    )
    runner = StubRunner(
        replies=[
            purge_reply(
                [{"message_id": "m1", "action": "label", "sender_category": "x"}]
            )
        ]
    )

    report = await env.sweep_service(transport, runner).sweep(logger=env.logger)

    assert_eq(report.proposed, 0)


# --- No direct writes -------------------------------------------------------


@test()
async def the_sweep_never_writes_the_mailbox() -> None:
    """The sweep only proposes: no modify/trash call ever reaches the transport."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page(["m1", "m2"])],
        messages={"m1": _message("m1"), "m2": _message("m2")},
    )
    runner = StubRunner(
        replies=[purge_reply([archive_verdict("m1"), delete_verdict("m2")])]
    )

    _ = await env.sweep_service(transport, runner).sweep(logger=env.logger)

    assert_eq(transport.modify_calls, [])
    assert_eq(transport.trash_calls, [])


# --- Watermark resume (separate key) ----------------------------------------


@test()
async def the_watermark_resumes_across_passes_incrementally() -> None:
    """A second sweep after a successful one bounds its query by the watermark."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page([]), _list_page([])],
        messages={},
    )
    service = env.sweep_service(transport, StubRunner(replies=[]))

    _ = await service.sweep(logger=env.logger)
    _ = await service.sweep(logger=env.logger)

    assert_true("after:" not in transport.list_calls[0])
    assert_true("after:" in transport.list_calls[1])


@test()
async def the_purge_watermark_is_independent_of_the_ingestion_watermark() -> None:
    """The sweep stores under its own key, not the ingestion `*_message_watermark`."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(message_pages=[_list_page([])], messages={})

    _ = await env.sweep_service(transport, StubRunner(replies=[])).sweep(
        logger=env.logger
    )

    async with env.database.transaction() as tx:
        rows = await tx.fetch_all(select(GmailSyncState).all())
    keys = {row.key for row in rows}
    assert_true("gmail_purge_watermark" in keys)
    assert_true("gmail_message_watermark" not in keys)


@test()
async def a_second_pass_still_scans_new_backlog() -> None:
    """After the watermark advances, a later pass still sweeps newly-listed mail."""
    env = await load_fixture(purge_env())
    transport = PurgeTransport(
        message_pages=[_list_page([]), _list_page(["m9"])],
        messages={"m9": _message("m9")},
    )
    runner = StubRunner(replies=[purge_reply([archive_verdict("m9")])])
    service = env.sweep_service(transport, runner)

    first = await service.sweep(logger=env.logger)
    second = await service.sweep(logger=env.logger)

    assert_eq(first.proposed, 0)
    assert_eq(second.proposed, 1)
