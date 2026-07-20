"""Behaviour tests for the Gmail read-only ingestion gate.

These drive `GmailClient` and `GmailSyncService` against a real in-memory
SQLite database and real `MemoryService`/`TriggerService`, faking only the
Gmail HTTP boundary (`FakeGmailTransport`) and the triage model
(`FakeTriageRunner`) — never a live Gmail or model call. They assert the full
"Key scenarios" list from the spec: the category pre-filter short-circuits
triage, a malformed batch entry leaves only that message pending, the `tether`
label overrides a noise verdict, a future deadline creates a trigger at the
correct (possibly clamped) fire time while a past deadline does not, a re-run
over the overlap window is idempotent, and a failed pass never persists its
watermark.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched
from snektest import (
    assert_eq,
    assert_false,
    assert_is_none,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.gmail import (
    GmailClient,
    GmailResponse,
    GmailSyncService,
    create_gmail_schema,
)
from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryService,
    create_memory_schema,
)
from tether.triggers import ScheduledTrigger, TriggerService, create_trigger_schema


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.gmail")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.gmail")


# --- Fake transport + triage runner -----------------------------------------


@dataclass
class FakeGmailTransport:
    """A scripted `GmailTransport`: canned pages/messages/labels, records calls."""

    message_pages: list[dict[str, object]]
    messages: dict[str, dict[str, object]]
    labels: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    list_calls: list[tuple[str, str | None]] = field(
        default_factory=list[tuple[str, str | None]]
    )

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        self.list_calls.append((query, page_token))
        return GmailResponse(status_code=200, payload=self.message_pages.pop(0))

    async def get_message(self, message_id: str) -> GmailResponse:
        return GmailResponse(status_code=200, payload=self.messages[message_id])

    async def list_labels(self) -> GmailResponse:
        return GmailResponse(status_code=200, payload={"labels": self.labels})


@dataclass
class FakeTriageRunner:
    """A scripted `GmailTriageRunner`: replies handed out in order, prompts recorded."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list[str])

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


class RaisingTriageRunner:
    """A `GmailTriageRunner` that always fails, for pass-failure tests."""

    async def run(self, prompt: str) -> str:
        message = "model unavailable"
        raise RuntimeError(message)


def _encode_body(text: str) -> str:
    """Base64url-encode a plaintext body the way Gmail's API returns it."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


_DEFAULT_INTERNAL_DATE = datetime(2026, 1, 1, tzinfo=UTC)
"""Default `internalDate` for `message_payload`, hoisted off the parameter list
so it is not constructed in a default expression (`reportCallInDefaultInitializer`)."""


def message_payload(  # noqa: PLR0913 (a builder mirroring the Gmail API's shape)
    message_id: str,
    *,
    from_header: str = "sender@example.com",
    subject: str = "Hello",
    date_header: str = "Mon, 1 Jan 2026 00:00:00 +0000",
    label_ids: Sequence[str] = (),
    body_text: str = "hello there",
    html_body: str | None = None,
    internal_date: datetime = _DEFAULT_INTERNAL_DATE,
) -> dict[str, object]:
    """Build one raw `messages.get` payload as the Gmail API shapes it."""
    if html_body is not None:
        body: Mapping[str, object] = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _encode_body(html_body)},
                }
            ],
        }
    else:
        body = {"mimeType": "text/plain", "body": {"data": _encode_body(body_text)}}
    return {
        "id": message_id,
        "threadId": message_id,
        "labelIds": list(label_ids),
        "internalDate": str(int(internal_date.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date_header},
            ],
            **body,
        },
    }


def message_list_page(
    message_ids: Sequence[str], *, next_page_token: str | None = None
) -> dict[str, object]:
    """Build one `messages.list` page listing the given ids."""
    return {
        "messages": [{"id": message_id} for message_id in message_ids],
        "nextPageToken": next_page_token,
    }


def verdict_reply(entries: Sequence[dict[str, object]]) -> str:
    """Build a triage reply: a bare JSON array of verdict objects."""
    return json.dumps(list(entries))


def noise_verdict(message_id: str) -> dict[str, object]:
    return {"message_id": message_id, "classification": "noise", "why": "bulk mail"}


def interesting_verdict(
    message_id: str,
    *,
    why: str = "worth remembering",
    deadline_at: str | None = None,
    deadline_description: str = "renewal",
    actionable: bool = False,
) -> dict[str, object]:
    verdict: dict[str, object] = {
        "message_id": message_id,
        "classification": "interesting",
        "why": why,
        "actionable": actionable,
    }
    if deadline_at is not None:
        verdict["deadline"] = {"at": deadline_at, "description": deadline_description}
    return verdict


# --- Fixture -----------------------------------------------------------------


@dataclass
class GmailEnv:
    """A Gmail-ready database plus live `MemoryService`/`TriggerService`."""

    database: Database
    memory_service: MemoryService
    trigger_service: TriggerService
    logger: Logger

    def sync_service(
        self,
        transport: FakeGmailTransport,
        triage_runner: FakeTriageRunner | RaisingTriageRunner,
        *,
        triage_batch_size: int = 10,
    ) -> GmailSyncService:
        """Wire a sync service over a scripted transport and triage runner."""
        return GmailSyncService(
            database=self.database,
            client=GmailClient(transport=transport),
            memory_service=self.memory_service,
            trigger_service=self.trigger_service,
            triage_runner=triage_runner,
            triage_batch_size=triage_batch_size,
        )

    async def tethered_memories(self) -> list[Memory[Fetched]]:
        """The current tethered corpus, for content/facet assertions."""
        return await self.memory_service.browse_by_state("tethered", logger=self.logger)

    async def triggers(self) -> list[ScheduledTrigger[Fetched]]:
        """The current live triggers, soonest-due first."""
        return await self.trigger_service.list_triggers(logger=self.logger)


@fixture
async def gmail_env() -> AsyncGenerator[GmailEnv]:
    """A fresh database with the Memory + Trigger + Gmail schema and a live KB dir."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_trigger_schema(db)
    await create_gmail_schema(db)
    async with TemporaryDirectory() as kb_root:
        yield GmailEnv(
            database=db,
            memory_service=MemoryService(
                database=db,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            trigger_service=TriggerService(database=db, tracer=noop_tracer()),
            logger=test_logger(),
        )
    await db.close()


# --- Category pre-filter -----------------------------------------------------


@test()
async def a_promotions_labeled_message_is_prefiltered_without_triage() -> None:
    """A CATEGORY_PROMOTIONS message is recorded prefiltered; triage never runs."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={
            "m1": message_payload("m1", label_ids=["CATEGORY_PROMOTIONS", "INBOX"])
        },
    )
    triage_runner = FakeTriageRunner(replies=[])

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.prefiltered, 1)
    assert_eq(triage_runner.prompts, [])
    assert_eq(await env.tethered_memories(), [])


@test()
async def a_social_labeled_message_is_prefiltered() -> None:
    """CATEGORY_SOCIAL is pre-filtered exactly like CATEGORY_PROMOTIONS."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1", label_ids=["CATEGORY_SOCIAL"])},
    )

    report = await env.sync_service(transport, FakeTriageRunner(replies=[])).sync(
        logger=env.logger
    )

    assert_eq(report.prefiltered, 1)


@test()
async def spam_trash_and_sent_are_excluded_entirely() -> None:
    """SPAM/TRASH/SENT-labeled messages produce no memory and are never triaged."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["spam", "trash", "sent"])],
        messages={
            "spam": message_payload("spam", label_ids=["SPAM"]),
            "trash": message_payload("trash", label_ids=["TRASH"]),
            "sent": message_payload("sent", label_ids=["SENT"]),
        },
    )
    triage_runner = FakeTriageRunner(replies=[])

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(triage_runner.prompts, [])
    assert_eq(report.ingested, 0)
    assert_eq(report.noise, 0)
    assert_eq(await env.tethered_memories(), [])


# --- Triage application ------------------------------------------------------


@test()
async def an_interesting_verdict_captures_a_tethered_memory() -> None:
    """An `interesting` verdict mints one tethered Memory with gmail provenance."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={
            "m1": message_payload(
                "m1", from_header="a@example.com", subject="Renewal", body_text="Body."
            )
        },
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1", why="Renewal due soon")])]
    )

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.ingested, 1)
    memories = await env.tethered_memories()
    assert_eq(len(memories), 1)
    assert_eq(memories[0].provenance, {"kind": "gmail"})
    assert_true(memories[0].content.startswith("Renewal due soon"))
    assert_eq(memories[0].facets["sender"], "a@example.com")
    assert_eq(memories[0].facets["subject"], "Renewal")


@test()
async def a_noise_verdict_creates_no_memory() -> None:
    """A `noise` verdict is recorded without capturing a Memory."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    triage_runner = FakeTriageRunner(replies=[verdict_reply([noise_verdict("m1")])])

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.noise, 1)
    assert_eq(await env.tethered_memories(), [])


@test()
async def an_actionable_verdict_facets_action_pending() -> None:
    """An actionable, undated verdict facets the Memory `action: pending`."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1", actionable=True)])]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.facets["action"], "pending")


@test()
async def a_malformed_entry_leaves_only_that_message_pending() -> None:
    """One malformed verdict in a batch leaves only that message pending."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1", "m2"])],
        messages={
            "m1": message_payload("m1"),
            "m2": message_payload("m2"),
        },
    )
    # m1 gets a well-formed verdict; m2's entry carries an invalid classification.
    triage_runner = FakeTriageRunner(
        replies=[
            verdict_reply(
                [
                    interesting_verdict("m1"),
                    {"message_id": "m2", "classification": "bogus", "why": "?"},
                ]
            )
        ]
    )

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.ingested, 1)
    assert_eq(report.pending, 1)
    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def a_missing_verdict_entry_leaves_that_message_pending() -> None:
    """A message with no corresponding verdict entry at all stays pending."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1", "m2"])],
        messages={"m1": message_payload("m1"), "m2": message_payload("m2")},
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.ingested, 1)
    assert_eq(report.pending, 1)


# --- `tether` label override -------------------------------------------------


@test()
async def a_tether_labeled_message_cannot_be_classified_noise() -> None:
    """A `tether`-labeled message's `noise` verdict is overridden to interesting."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1", label_ids=["Label_1"])},
        labels=[{"id": "Label_1", "name": "tether"}],
    )
    triage_runner = FakeTriageRunner(replies=[verdict_reply([noise_verdict("m1")])])

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.ingested, 1)
    assert_eq(report.noise, 0)
    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def a_tether_labeled_message_bypasses_the_category_prefilter() -> None:
    """A `tether`-labeled promotions message still reaches triage."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={
            "m1": message_payload("m1", label_ids=["CATEGORY_PROMOTIONS", "Label_1"])
        },
        labels=[{"id": "Label_1", "name": "tether"}],
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    report = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(report.prefiltered, 0)
    assert_eq(report.ingested, 1)


# --- Deadlines and triggers ---------------------------------------------------


@test()
async def a_future_deadline_creates_a_trigger() -> None:
    """A future deadline verdict additionally creates a one-shot trigger."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={
            "m1": message_payload(
                "m1", from_header="billing@example.com", subject="Invoice due"
            )
        },
    )
    triage_runner = FakeTriageRunner(
        replies=[
            verdict_reply(
                [
                    interesting_verdict(
                        "m1",
                        deadline_at="2030-06-15T12:00:00+00:00",
                        deadline_description="Invoice payment",
                    )
                ]
            )
        ]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    triggers = await env.triggers()
    assert_eq(len(triggers), 1)
    assert_eq(triggers[0].recurrence, "once")
    assert_eq(
        triggers[0].next_fire_at,
        datetime(2030, 6, 14, 9, 0, tzinfo=UTC),
    )
    assert_true("Invoice payment" in triggers[0].payload)
    assert_true("billing@example.com" in triggers[0].payload)
    assert_true("Invoice due" in triggers[0].payload)


@test()
async def a_near_deadline_clamps_the_fire_time_to_near_now() -> None:
    """A deadline whose morning-before slot has passed clamps the fire time."""
    env = await load_fixture(gmail_env())
    now = datetime.now(UTC)
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    triage_runner = FakeTriageRunner(
        replies=[
            verdict_reply(
                [
                    interesting_verdict(
                        "m1",
                        deadline_at=(now + timedelta(hours=2)).isoformat(),
                        deadline_description="Same-day deadline",
                    )
                ]
            )
        ]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    triggers = await env.triggers()
    assert_eq(len(triggers), 1)
    assert_true(triggers[0].next_fire_at > now)
    assert_true(triggers[0].next_fire_at < now + timedelta(minutes=30))


@test()
async def a_past_deadline_creates_a_memory_but_no_trigger() -> None:
    """A deadline already in the past creates the Memory but skips the trigger."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    triage_runner = FakeTriageRunner(
        replies=[
            verdict_reply(
                [
                    interesting_verdict(
                        "m1",
                        deadline_at="2020-01-01T00:00:00+00:00",
                        deadline_description="Long past",
                    )
                ]
            )
        ]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_eq(len(await env.tethered_memories()), 1)
    assert_eq(await env.triggers(), [])


# --- Idempotency + watermark ---------------------------------------------------


@test()
async def an_already_recorded_message_is_not_reprocessed() -> None:
    """A re-run listing the same id (overlap window) creates nothing new."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"]), message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )
    service = env.sync_service(transport, triage_runner)

    first = await service.sync(logger=env.logger)
    second = await service.sync(logger=env.logger)

    assert_eq(first.ingested, 1)
    assert_eq(second.ingested, 0)
    assert_eq(second.noise, 0)
    assert_eq(second.pending, 0)
    assert_eq(len(await env.tethered_memories()), 1)
    # The second pass triaged nothing new, so the runner was never called again.
    assert_eq(len(triage_runner.prompts), 1)


@test()
async def a_successful_pass_runs_the_next_query_incrementally() -> None:
    """A second pass after a successful one bounds its query by the watermark."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page([]), message_list_page([])],
        messages={},
    )
    service = env.sync_service(transport, FakeTriageRunner(replies=[]))

    _ = await service.sync(logger=env.logger)
    _ = await service.sync(logger=env.logger)

    assert_is_none(_query_of(transport.list_calls[0]))
    assert_true(_query_of(transport.list_calls[1]) is not None)
    assert_true("after:" in (transport.list_calls[1][0]))


def _query_of(call: tuple[str, str | None]) -> str | None:
    """Extract the `after:` presence signal from a recorded list call's query."""
    return call[0] if "after:" in call[0] else None


@test()
async def a_failed_pass_does_not_persist_the_watermark() -> None:
    """A raising triage runner fails the whole pass; the next pass retries fully."""
    env = await load_fixture(gmail_env())
    failing_transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    service = env.sync_service(failing_transport, RaisingTriageRunner())

    with assert_raises(RuntimeError):
        _ = await service.sync(logger=env.logger)

    # A second pass, with a working runner, still sends a full (non-incremental)
    # query and re-lists the never-recorded message — proof the watermark from
    # the failed pass was never persisted.
    recovering_transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1")},
    )
    service.client = GmailClient(transport=recovering_transport)
    service.triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    report = await service.sync(logger=env.logger)

    assert_is_none(_query_of(recovering_transport.list_calls[0]))
    assert_eq(report.ingested, 1)


# --- MIME body extraction ------------------------------------------------------


@test()
async def a_text_plain_body_is_used_verbatim() -> None:
    """A text/plain body is used as-is (up to truncation)."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1", body_text="Plain body text.")},
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    assert_true("Plain body text." in triage_runner.prompts[0])


@test()
async def an_html_only_body_is_tag_stripped() -> None:
    """A message with no text/plain part falls back to tag-stripped HTML."""
    env = await load_fixture(gmail_env())
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={
            "m1": message_payload(
                "m1", html_body="<p>Hello <b>world</b></p>", body_text=""
            )
        },
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    prompt = triage_runner.prompts[0]
    assert_true("Hello world" in prompt)
    assert_false("<p>" in prompt)
    assert_false("<b>" in prompt)


@test()
async def a_long_body_is_truncated() -> None:
    """A body far longer than the truncation bound is cut down before triage."""
    env = await load_fixture(gmail_env())
    long_body = "x" * 10_000
    transport = FakeGmailTransport(
        message_pages=[message_list_page(["m1"])],
        messages={"m1": message_payload("m1", body_text=long_body)},
    )
    triage_runner = FakeTriageRunner(
        replies=[verdict_reply([interesting_verdict("m1")])]
    )

    _ = await env.sync_service(transport, triage_runner).sync(logger=env.logger)

    prompt = triage_runner.prompts[0]
    assert_true(len(prompt) < len(long_body) + 2_000)


# --- Batching -------------------------------------------------------------


@test()
async def eligible_messages_are_triaged_in_bounded_batches() -> None:
    """More eligible messages than the batch size are triaged over several runs."""
    env = await load_fixture(gmail_env())
    ids = [f"m{i}" for i in range(3)]
    transport = FakeGmailTransport(
        message_pages=[message_list_page(ids)],
        messages={message_id: message_payload(message_id) for message_id in ids},
    )
    triage_runner = FakeTriageRunner(
        replies=[
            verdict_reply([interesting_verdict("m0")]),
            verdict_reply([interesting_verdict("m1")]),
            verdict_reply([interesting_verdict("m2")]),
        ]
    )

    report = await env.sync_service(transport, triage_runner, triage_batch_size=1).sync(
        logger=env.logger
    )

    assert_eq(report.ingested, 3)
    assert_eq(len(triage_runner.prompts), 3)
