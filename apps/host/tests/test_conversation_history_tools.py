"""Behaviour tests for the `/internal/tools/read_conversation_history` tool.

Drives the mounted Starlette app through `TestClient`, exercising the same
auth gate and envelope as the other internal tools. The prior-session
transcript is seeded directly through `ConversationService` (via the sync
`TestClient.portal`) rather than a live pi run, so each test controls exactly
which rows land on which side of the session boundary.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID

from snekql.sqlite import update
from snektest import assert_eq, assert_len, assert_not_in, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether.conversations import ConversationService, Message, MessageDraft
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"


def make_client(root: Path) -> TestClient:
    """A test app with an isolated DB/KB and a known tool secret."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret="test-session-secret",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
            tool_secret=SECRET,
        )
    )


def call(
    client: TestClient, session_id: str, tool: str, **params: Any
) -> dict[str, Any]:
    """Invoke a tool with the known secret, returning the raw envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": session_id, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


async def _age_conversation(
    service: ConversationService, conversation_id: UUID, minutes: int
) -> None:
    """Push every existing row `minutes` into the past, opening a cold gap."""
    stale = (datetime.now(UTC) - timedelta(minutes=minutes)).replace(tzinfo=None)
    async with service.database.transaction() as tx:
        _ = await tx.execute(
            update(Message)
            .set(Message.created_at.to(stale))
            .where(Message.conversation_id.eq(conversation_id))
        )


def _seed(client: TestClient) -> tuple[str, str]:
    """Build one conversation with an aged prior session and a live one.

    Prior session (backdated ~10 minutes, past the 5-minute gap): a user
    question, an assistant reply, a tool call carrying a large payload, and a
    reasoning row. Live session (now): one fresh user/assistant exchange.

    Returns `(session_id, conversation_id)` — `session_id` is registered so
    it authenticates as the live pi session, matching how a real tool call's
    `session_id` is the conversation's current `pi_session_id`.
    """
    app = cast("Starlette", client.app)
    service = cast("ConversationService", app.state.conversation_service)
    portal = client.portal
    assert portal is not None

    conversation = portal.call(service.list_conversations)[0]
    conversation_id = conversation.id

    for draft in (
        MessageDraft(
            content="what's the weather", conversation_id=conversation_id, role="user"
        ),
        MessageDraft(
            content="sunny today", conversation_id=conversation_id, role="assistant"
        ),
        MessageDraft(
            content="search",
            conversation_id=conversation_id,
            role="tool",
            tool_name="search",
            tool_args={"q": "weather"},
            tool_result={"huge": "x" * 5000},
        ),
        MessageDraft(
            content="thinking about weather APIs at length" * 50,
            conversation_id=conversation_id,
            role="reasoning",
        ),
    ):
        _ = portal.call(service.append_message, draft)
    portal.call(_age_conversation, service, conversation_id, 10)

    for draft in (
        MessageDraft(
            content="ok but what about tomorrow",
            conversation_id=conversation_id,
            role="user",
        ),
        MessageDraft(
            content="rain tomorrow", conversation_id=conversation_id, role="assistant"
        ),
    ):
        _ = portal.call(service.append_message, draft)

    session_id = str(conversation.pi_session_id)
    cast("SessionRegistry", app.state.session_registry).register(session_id)
    return session_id, str(conversation_id)


@test()
def read_conversation_history_without_secret_is_rejected() -> None:
    """A call lacking the per-process secret never reaches the tool."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        session_id, _ = _seed(client)
        response = client.post(
            "/internal/tools/read_conversation_history",
            json={"session_id": session_id},
        )

    assert_eq(response.status_code, 401)


@test()
def read_conversation_history_with_unknown_session_is_rejected() -> None:
    """Identity must resolve to a registered session id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/read_conversation_history",
            json={"session_id": "ghost"},
            headers={SECRET_HEADER: SECRET},
        )

    assert_eq(response.status_code, 401)


@test()
def read_conversation_history_returns_empty_when_the_conversation_never_rotated() -> (
    None
):
    """No cold gap means the whole transcript is the live session."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        app = cast("Starlette", client.app)
        service = cast("ConversationService", app.state.conversation_service)
        portal = client.portal
        assert portal is not None
        conversation = portal.call(service.list_conversations)[0]
        _ = portal.call(
            service.append_message,
            MessageDraft(content="hello", conversation_id=conversation.id, role="user"),
        )
        session_id = str(conversation.pi_session_id)
        cast("SessionRegistry", app.state.session_registry).register(session_id)

        envelope = call(client, session_id, "read_conversation_history")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"], [])


@test()
def read_conversation_history_shapes_prior_session_rows() -> None:
    """Tool rows collapse to a marker, reasoning is dropped, order is kept."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        session_id, _ = _seed(client)
        envelope = call(client, session_id, "read_conversation_history")

    assert_eq(envelope["success"], True)
    entries = envelope["result"]
    assert_len(entries, 3)
    assert_eq(
        [entry["role"] for entry in entries],
        ["user", "assistant", "tool"],
    )
    assert_eq(entries[0]["content"], "what's the weather")
    assert_eq(entries[1]["content"], "sunny today")
    assert_eq(entries[2]["content"], "used search")
    for entry in entries:
        assert_not_in("tool_args", entry)
        assert_not_in("tool_result", entry)
        assert_true("created_at" in entry)


@test()
def read_conversation_history_excludes_the_live_session() -> None:
    """Rows from after the cold gap never come back — pi already has them."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        session_id, _ = _seed(client)
        envelope = call(client, session_id, "read_conversation_history")

    contents = {entry["content"] for entry in envelope["result"]}
    assert_not_in("ok but what about tomorrow", contents)
    assert_not_in("rain tomorrow", contents)


@test()
def read_conversation_history_truncates_long_content() -> None:
    """A very long user/assistant row is capped, not returned in full."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        app = cast("Starlette", client.app)
        service = cast("ConversationService", app.state.conversation_service)
        portal = client.portal
        assert portal is not None
        conversation = portal.call(service.list_conversations)[0]
        conversation_id = conversation.id
        long_content = "a" * 5000
        _ = portal.call(
            service.append_message,
            MessageDraft(
                content=long_content, conversation_id=conversation_id, role="user"
            ),
        )
        portal.call(_age_conversation, service, conversation_id, 10)
        _ = portal.call(
            service.append_message,
            MessageDraft(
                content="new turn", conversation_id=conversation_id, role="user"
            ),
        )
        session_id = str(conversation.pi_session_id)
        cast("SessionRegistry", app.state.session_registry).register(session_id)

        envelope = call(client, session_id, "read_conversation_history")

    entry = envelope["result"][0]
    assert_true(len(entry["content"]) < len(long_content))
    assert_true(entry["content"].endswith("[truncated]"))


@test()
def read_conversation_history_rejects_a_limit_over_the_cap() -> None:
    """`limit` beyond the cap fails validation rather than flooding context."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        session_id, _ = _seed(client)
        envelope = call(client, session_id, "read_conversation_history", limit=1000)

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


def _seed_four_prior_rows(client: TestClient) -> str:
    """Four prior-session user rows (`one`..`four`) plus a live-session row.

    Distinct from `_seed`, which mixes roles to test output shaping; this
    seed keeps every prior row a plain `user` message so pagination can
    assert on content order without any rows being filtered out.
    """
    app = cast("Starlette", client.app)
    service = cast("ConversationService", app.state.conversation_service)
    portal = client.portal
    assert portal is not None

    conversation = portal.call(service.list_conversations)[0]
    conversation_id = conversation.id
    for content in ("one", "two", "three", "four"):
        _ = portal.call(
            service.append_message,
            MessageDraft(content=content, conversation_id=conversation_id, role="user"),
        )
    portal.call(_age_conversation, service, conversation_id, 10)
    _ = portal.call(
        service.append_message,
        MessageDraft(content="live turn", conversation_id=conversation_id, role="user"),
    )
    session_id = str(conversation.pi_session_id)
    cast("SessionRegistry", app.state.session_registry).register(session_id)
    return session_id


@test()
def read_conversation_history_before_cursor_walks_further_back() -> None:
    """`before` pages to the window just older than the previous page."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        session_id = _seed_four_prior_rows(client)

        newest_page = call(client, session_id, "read_conversation_history", limit=2)[
            "result"
        ]
        oldest_seq_seen = min(entry["seq"] for entry in newest_page)
        older_page = call(
            client,
            session_id,
            "read_conversation_history",
            limit=2,
            before=oldest_seq_seen,
        )["result"]

    assert_eq([entry["content"] for entry in newest_page], ["three", "four"])
    assert_eq([entry["content"] for entry in older_page], ["one", "two"])
