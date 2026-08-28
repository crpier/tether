"""Authenticated Evidence-reference resolution through the browser seam."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from snekql.sqlite import Database, Fetched, insert
from snektest import assert_eq, assert_true, test
from starlette.applications import Starlette

from tests.surfaces import login, surface_client
from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft
from tether.conversation_store import ConversationTurn
from tether.email_evidence_store import EmailEvidenceSnapshot
from tether.health_connect.persistence import (
    HcExerciseLap,
    HcExerciseSegment,
    HcExerciseSession,
    HcSleepSession,
    HcSleepStage,
)

_BASE_MILLIS = 1_700_000_000_000
_HOUR_MILLIS = 3_600_000


async def _seed_email_evidence(
    database: Database,
) -> EmailEvidenceSnapshot[Fetched]:
    """Insert one promoted source as setup for Evidence inspection."""
    async with database.transaction(mode="immediate") as transaction:
        return await transaction.execute(
            insert(
                EmailEvidenceSnapshot(
                    body_chars=39,
                    body_text="The apartment is booked for 12-18 June.",
                    body_truncated=False,
                    content_hash="known-source-hash",
                    date_header="Tue, 7 Apr 2026 09:30:00 +0000",
                    from_header="Alice <alice@example.com>",
                    gmail_message_id="m1",
                    subject="Lisbon booking",
                    thread_id="t1",
                )
            ).returning()
        )


async def _seed_exercise(database: Database) -> int:
    """Insert one exercise episode with segment and lap structure."""
    async with database.transaction(mode="immediate") as transaction:
        episode = await transaction.execute(
            insert(
                HcExerciseSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=_BASE_MILLIS + _HOUR_MILLIS,
                    end_zone_offset_seconds=0,
                    exercise_type=56,
                    is_deleted=False,
                    modified_at=_BASE_MILLIS,
                    notes=None,
                    origin_id=None,
                    payload_hash="exercise-hash",
                    planned_exercise_session_id=None,
                    received_at=_BASE_MILLIS,
                    recording_method=2,
                    record_uid="exercise-record",
                    request_id="exercise-request",
                    start_time=_BASE_MILLIS,
                    start_zone_offset_seconds=0,
                    title="Morning run",
                )
            ).returning()
        )
        _ = await transaction.execute(
            insert(
                HcExerciseSegment(
                    end_time=_BASE_MILLIS + _HOUR_MILLIS,
                    repetitions_count=0,
                    segment_index=0,
                    segment_type=1,
                    start_time=_BASE_MILLIS,
                    version_id=episode.version_id,
                )
            )
        )
        for index, length in enumerate((1000.0, 500.0)):
            _ = await transaction.execute(
                insert(
                    HcExerciseLap(
                        end_time=_BASE_MILLIS + (index + 1) * 30 * 60_000,
                        lap_index=index,
                        length_meters=length,
                        start_time=_BASE_MILLIS + index * 30 * 60_000,
                        version_id=episode.version_id,
                    )
                )
            )
    return episode.version_id


async def _seed_historical_sleep(database: Database) -> int:
    """Insert one cited sleep version followed by a newer edit."""
    async with database.transaction(mode="immediate") as transaction:
        cited = await transaction.execute(
            insert(
                HcSleepSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=_BASE_MILLIS + 8 * _HOUR_MILLIS,
                    end_zone_offset_seconds=0,
                    is_deleted=False,
                    modified_at=_BASE_MILLIS,
                    notes=None,
                    origin_id=None,
                    payload_hash="cited-hash",
                    received_at=_BASE_MILLIS,
                    recording_method=2,
                    record_uid="sleep-record",
                    request_id="cited-request",
                    start_time=_BASE_MILLIS,
                    start_zone_offset_seconds=0,
                    title="Night sleep",
                )
            ).returning()
        )
        _ = await transaction.execute(
            insert(
                HcSleepStage(
                    end_time=_BASE_MILLIS + 3 * _HOUR_MILLIS,
                    stage=5,
                    stage_index=0,
                    start_time=_BASE_MILLIS,
                    version_id=cited.version_id,
                )
            )
        )
        _ = await transaction.execute(
            insert(
                HcSleepStage(
                    end_time=_BASE_MILLIS + 8 * _HOUR_MILLIS,
                    stage=6,
                    stage_index=1,
                    start_time=_BASE_MILLIS + 3 * _HOUR_MILLIS,
                    version_id=cited.version_id,
                )
            )
        )
        _ = await transaction.execute(
            insert(
                HcSleepSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=_BASE_MILLIS + 6 * _HOUR_MILLIS,
                    end_zone_offset_seconds=0,
                    is_deleted=False,
                    modified_at=_BASE_MILLIS + 1,
                    notes=None,
                    origin_id=None,
                    payload_hash="newer-hash",
                    received_at=_BASE_MILLIS + 1,
                    recording_method=2,
                    record_uid="sleep-record",
                    request_id="newer-request",
                    start_time=_BASE_MILLIS,
                    start_zone_offset_seconds=0,
                    title="Edited sleep",
                )
            )
        )
    return cited.version_id


@test()
def missing_and_malformed_references_fail_clearly() -> None:
    """The resolver distinguishes auth, unsupported syntax, and absent Evidence."""
    missing = "tether://message/019f0000-0000-7000-8000-000000000099"
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        unauthorized = client.get("/api/evidence", params={"uri": missing})
        login(client)
        malformed = client.get(
            "/api/evidence", params={"uri": "tether://unknown/source"}
        )
        absent = client.get("/api/evidence", params={"uri": missing})

    assert_eq(unauthorized.status_code, 401)
    assert_eq(malformed.status_code, 422)
    assert_eq(malformed.json(), {"detail": "unsupported Evidence reference"})
    assert_eq(absent.status_code, 404)
    assert_eq(absent.json(), {"detail": "Evidence is unavailable"})


@test()
def message_reference_resolves_to_its_original_evidence() -> None:
    """A Message citation opens the exact settled source row it names."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
        portal = client.portal
        assert portal is not None
        message = portal.call(
            app_runtime(
                cast("Starlette", client.app)
            ).conversation_service.append_message,
            MessageDraft(
                content="I prefer aisle seats on overnight flights.",
                conversation_id=conversation_id,
                role="user",
            ),
        )

        uri = f"tether://message/{message.id}"
        response = client.get("/api/evidence", params={"uri": uri})

    assert_eq(response.status_code, 200)
    payload = response.json()
    assert_eq(payload["kind"], "message")
    assert_eq(payload["uri"], uri)
    assert_eq(payload["conversation_id"], str(conversation_id))
    assert_eq(payload["message_id"], str(message.id))
    assert_eq(payload["seq"], 1)
    assert_eq(payload["role"], "user")
    assert_eq(payload["content"], "I prefer aisle seats on overnight flights.")
    assert_true(payload["occurred_at"].endswith("Z"))


@test()
def final_assistant_reference_resolves_to_its_conclusion() -> None:
    """An eligible assistant citation opens the exact settled conclusion."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
        portal = client.portal
        assert portal is not None

        async def seed_conclusion() -> tuple[UUID, UUID]:
            async with runtime.conversation_service.database.transaction() as tx:
                turn = await tx.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=conversation_id,
                            origin="scheduled",
                            status="succeeded",
                            turn_seq=1,
                        )
                    ).returning()
                )
            conclusion = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="Recent viewing favors long-form technical interviews.",
                    conversation_id=conversation_id,
                    role="assistant",
                    turn_id=turn.id,
                )
            )
            return turn.id, conclusion.id

        _, conclusion_id = portal.call(seed_conclusion)
        uri = f"tether://message/{conclusion_id}"
        response = client.get("/api/evidence", params={"uri": uri})

    assert_eq(response.status_code, 200)
    payload = response.json()
    assert_eq(payload["kind"], "message")
    assert_eq(payload["uri"], uri)
    assert_eq(payload["role"], "assistant")
    assert_eq(
        payload["content"],
        "Recent viewing favors long-form technical interviews.",
    )


@test()
def failed_assistant_reference_is_not_evidence() -> None:
    """Partial assistant output from a failed turn cannot support Memory."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
        portal = client.portal
        assert portal is not None

        async def seed_partial_output() -> UUID:
            async with runtime.conversation_service.database.transaction() as tx:
                turn = await tx.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=conversation_id,
                            origin="health",
                            status="failed",
                            turn_seq=1,
                        )
                    ).returning()
                )
            message = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="This conclusion was interrupted.",
                    conversation_id=conversation_id,
                    role="assistant",
                    turn_id=turn.id,
                )
            )
            return message.id

        message_id = portal.call(seed_partial_output)
        response = client.get(
            "/api/evidence",
            params={"uri": f"tether://message/{message_id}"},
        )

    assert_eq(response.status_code, 404)


@test()
def earlier_assistant_reference_is_not_evidence() -> None:
    """Only the last assistant Message in a succeeded turn is authoritative."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
        portal = client.portal
        assert portal is not None

        async def seed_two_answers() -> UUID:
            async with runtime.conversation_service.database.transaction() as tx:
                turn = await tx.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=conversation_id,
                            origin="interactive",
                            status="succeeded",
                            turn_seq=1,
                        )
                    ).returning()
                )
            earlier = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="I will inspect the source first.",
                    conversation_id=conversation_id,
                    role="assistant",
                    turn_id=turn.id,
                )
            )
            _ = await runtime.conversation_service.append_message(
                MessageDraft(
                    content="The final source-backed conclusion.",
                    conversation_id=conversation_id,
                    role="assistant",
                    turn_id=turn.id,
                )
            )
            return earlier.id

        earlier_id = portal.call(seed_two_answers)
        response = client.get(
            "/api/evidence",
            params={"uri": f"tether://message/{earlier_id}"},
        )

    assert_eq(response.status_code, 404)


@test()
def email_reference_resolves_the_immutable_local_snapshot() -> None:
    """An email citation remains inspectable through its host-owned source."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        portal = client.portal
        assert portal is not None
        snapshot = portal.call(
            _seed_email_evidence,
            runtime.conversation_service.database,
        )

        uri = f"tether://email/{snapshot.id}"
        response = client.get("/api/evidence", params={"uri": uri})

    assert_eq(response.status_code, 200)
    payload = response.json()
    assert_eq(payload["kind"], "email")
    assert_eq(payload["uri"], uri)
    assert_eq(payload["gmail_message_id"], "m1")
    assert_eq(payload["thread_id"], "t1")
    assert_eq(payload["from_header"], "Alice <alice@example.com>")
    assert_eq(payload["date_header"], "Tue, 7 Apr 2026 09:30:00 +0000")
    assert_eq(payload["subject"], "Lisbon booking")
    assert_eq(payload["body_chars"], 39)
    assert_eq(payload["body_text"], "The apartment is booked for 12-18 June.")
    assert_eq(payload["body_truncated"], False)
    assert_eq(payload["content_hash"], "known-source-hash")
    assert_true(payload["captured_at"].endswith("Z"))


@test()
def sleep_reference_resolves_the_exact_historical_episode() -> None:
    """A Health citation keeps resolving the named version after source edits."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        portal = client.portal
        assert portal is not None
        version_id = portal.call(
            _seed_historical_sleep,
            runtime.health_connect_ingestion.database,
        )

        uri = f"tether://health-connect/sleep/sleep-record@v{version_id}"
        response = client.get("/api/evidence", params={"uri": uri})

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "kind": "health_connect_sleep",
            "uri": uri,
            "record_uid": "sleep-record",
            "version_id": version_id,
            "title": "Night sleep",
            "start_time": "2023-11-14T22:13:20Z",
            "end_time": "2023-11-15T06:13:20Z",
            "duration_minutes": 480.0,
            "stage_minutes": {"deep": 180.0, "rem": 300.0},
        },
    )


@test()
def exercise_reference_resolves_episode_structure() -> None:
    """An exercise citation exposes the exact session's deterministic structure."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        portal = client.portal
        assert portal is not None
        version_id = portal.call(
            _seed_exercise,
            runtime.health_connect_ingestion.database,
        )

        uri = f"tether://health-connect/exercise/exercise-record@v{version_id}"
        response = client.get("/api/evidence", params={"uri": uri})

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "kind": "health_connect_exercise",
            "uri": uri,
            "record_uid": "exercise-record",
            "version_id": version_id,
            "title": "Morning run",
            "start_time": "2023-11-14T22:13:20Z",
            "end_time": "2023-11-14T23:13:20Z",
            "duration_minutes": 60.0,
            "exercise_type": "running",
            "segment_count": 1,
            "lap_count": 2,
            "total_lap_meters": 1500.0,
        },
    )
