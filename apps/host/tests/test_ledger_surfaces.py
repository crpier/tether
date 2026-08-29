"""Tool and HTTP behavior for generic Ledgers."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID, uuid7

from snektest import assert_eq, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.surfaces import SESSION, call_tool, login, surface_client
from tether.agent_trace_model import RunCorrelation
from tether.app_runtime import app_runtime
from tether.conversation_model import MessageDraft


def _begin_interactive_turn(client: TestClient, wording: str) -> UUID:
    """Create the exact active user Evidence required by Ledger mutations."""
    runtime = app_runtime(cast("Starlette", client.app))
    conversation_id = UUID(client.get("/api/conversations").json()[0]["id"])
    if client.portal is None:
        raise RuntimeError("test client portal is not running")
    turn_id = uuid7()
    source = client.portal.call(
        runtime.conversation_service.append_message,
        MessageDraft(
            content=wording,
            conversation_id=conversation_id,
            role="user",
            turn_id=turn_id,
        ),
    )
    _ = runtime.trace_recorder.begin_run(
        session_id=SESSION,
        kind="conversation",
        prompt=wording,
        correlation=RunCorrelation(
            conversation_id=str(conversation_id),
            origin="interactive",
            turn_id=str(turn_id),
        ),
    )
    return UUID(str(source.id))


def _create_approved_ledger(client: TestClient) -> dict[str, object]:
    """Create one active Ledger through the confirmed public approval flow."""
    _begin_interactive_turn(client, "Propose an observation log.")
    proposal = call_tool(
        client,
        "propose_ledger",
        fields=[
            {
                "description": "The observed text.",
                "field_id": "observation",
                "label": "Observation",
                "required": True,
                "type": "text",
            }
        ],
        name="Observation log",
        purpose="Record repeated observations.",
    )["result"]
    _begin_interactive_turn(client, f"Approve Ledger proposal {proposal['id']}.")
    return call_tool(
        client,
        "approve_ledger_proposal",
        proposal_id=proposal["id"],
    )["result"]


@test()
def proposed_ledger_is_visible_for_user_approval() -> None:
    """An active Conversation can freeze one exact inspectable proposal."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_interactive_turn(client, "Propose a structured log for my observations.")

        envelope = call_tool(
            client,
            "propose_ledger",
            fields=[
                {
                    "description": "The observation's short description.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                }
            ],
            name="Observation log",
            purpose="Record repeated observations outside established Verticals.",
        )

        assert_true(envelope["success"])
        proposal = envelope["result"]
        assert_eq(proposal["kind"], "create")
        assert_eq(proposal["status"], "pending")
        response = client.get("/api/ledger-proposals")
        assert_eq(response.status_code, 200)
        assert_eq([item["id"] for item in response.json()], [proposal["id"]])


@test()
def later_user_message_approves_the_exact_ledger_proposal() -> None:
    """Approval creates revision one without accepting replacement schema input."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_interactive_turn(client, "Propose an observation log.")
        proposal = call_tool(
            client,
            "propose_ledger",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                }
            ],
            name="Observation log",
            purpose="Record repeated observations.",
        )["result"]
        _begin_interactive_turn(client, f"Approve Ledger proposal {proposal['id']}.")

        envelope = call_tool(
            client,
            "approve_ledger_proposal",
            proposal_id=proposal["id"],
        )

        assert_true(envelope["success"])
        ledger = envelope["result"]
        assert_eq(ledger["id"], proposal["ledger_id"])
        assert_eq(ledger["revision"], 1)
        assert_eq(ledger["name"], "Observation log")
        response = client.get("/api/ledgers")
        assert_eq(response.status_code, 200)
        assert_eq([item["id"] for item in response.json()], [ledger["id"]])


@test()
def approved_ledger_can_receive_an_exact_revision_proposal() -> None:
    """Evolution freezes a complete successor against an observed revision."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Add an optional rating to that Ledger.")

        envelope = call_tool(
            client,
            "propose_ledger_revision",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                },
                {
                    "description": "An optional whole-number rating.",
                    "field_id": "rating",
                    "label": "Rating",
                    "required": False,
                    "type": "integer",
                },
            ],
            ledger_id=ledger["id"],
            name="Observation log",
            purpose="Record repeated observations.",
            revision=ledger["revision"],
            status="active",
        )

        assert_true(envelope["success"])
        proposal = envelope["result"]
        assert_eq(proposal["kind"], "revise")
        assert_eq(proposal["base_revision"], 1)
        assert_eq(proposal["proposed_revision"], 2)


@test()
def approved_revision_preserves_the_prior_ledger_interpretation() -> None:
    """Approving a successor retains both immutable schema versions."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Add an optional rating to that Ledger.")
        proposal = call_tool(
            client,
            "propose_ledger_revision",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                },
                {
                    "description": "An optional whole-number rating.",
                    "field_id": "rating",
                    "label": "Rating",
                    "required": False,
                    "type": "integer",
                },
            ],
            ledger_id=ledger["id"],
            name="Observation log",
            purpose="Record repeated observations.",
            revision=ledger["revision"],
            status="active",
        )["result"]
        _begin_interactive_turn(client, f"Approve Ledger proposal {proposal['id']}.")

        approved = call_tool(
            client,
            "approve_ledger_proposal",
            proposal_id=proposal["id"],
        )

        assert_true(approved["success"])
        assert_eq(approved["result"]["revision"], 2)
        response = client.get(f"/api/ledgers/{ledger['id']}")
        assert_eq(response.status_code, 200)
        detail = response.json()
        assert_eq([item["revision"] for item in detail["revisions"]], [2, 1])
        assert_eq(len(detail["revisions"][0]["fields"]), 2)
        assert_eq(len(detail["revisions"][1]["fields"]), 1)


@test()
def active_user_message_can_append_a_schema_valid_ledger_entry() -> None:
    """A bounded write records values, schema version, time, and exact Evidence."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        source_id = _begin_interactive_turn(client, "Log observation: clear sky.")

        envelope = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {
                    "occurred_at": "2026-08-29T09:30:00Z",
                    "values": {"observation": "clear sky"},
                }
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        assert_true(envelope["success"])
        entry = envelope["result"][0]
        assert_eq(entry["values"], {"observation": "clear sky"})
        assert_eq(entry["revision"], 1)
        assert_eq(entry["evidence"], [f"tether://message/{source_id}"])
        resolved = client.get(
            "/api/evidence",
            params={"uri": entry["evidence"][0]},
        )
        assert_eq(resolved.status_code, 200)
        assert_eq(resolved.json()["message_id"], str(source_id))
        response = client.get(f"/api/ledgers/{ledger['id']}/entries")
        assert_eq(response.status_code, 200)
        assert_eq([item["id"] for item in response.json()], [entry["id"]])


@test()
def correction_supersedes_one_entry_without_rewriting_history() -> None:
    """Default reads show the replacement while history retains both records."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log observation: clear sky.")
        original = call_tool(
            client,
            "append_ledger_entries",
            entries=[{"values": {"observation": "clear sky"}}],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )["result"][0]
        _begin_interactive_turn(client, "Correction: the sky was overcast.")

        corrected = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {
                    "supersedes_entry_id": original["id"],
                    "values": {"observation": "overcast"},
                }
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        assert_true(corrected["success"])
        replacement = corrected["result"][0]
        assert_eq(replacement["supersedes_entry_id"], original["id"])
        current = client.get(f"/api/ledgers/{ledger['id']}/entries").json()
        assert_eq([item["id"] for item in current], [replacement["id"]])
        history = client.get(
            f"/api/ledgers/{ledger['id']}/entries?include_superseded=true"
        ).json()
        assert_eq([item["id"] for item in history], [replacement["id"], original["id"]])
        assert_eq(history[1]["superseded_by_entry_id"], replacement["id"])


@test()
def ledger_query_filters_current_entries_by_text() -> None:
    """Dedicated Ledger query reads records without broadening Memory Search."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log clear sky and heavy rain observations.")
        _ = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {"values": {"observation": "clear sky"}},
                {"values": {"observation": "heavy rain"}},
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        queried = call_tool(
            client,
            "query_ledger_entries",
            ledger_id=ledger["id"],
            q="rain",
        )

        assert_true(queried["success"])
        assert_eq(
            [entry["values"] for entry in queried["result"]],
            [{"observation": "heavy rain"}],
        )


@test()
def ledger_query_filters_by_occurrence_time() -> None:
    """Time bounds use occurrence time when the record supplies one."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log yesterday and today's observations.")
        _ = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {
                    "occurred_at": "2026-08-28T09:00:00Z",
                    "values": {"observation": "yesterday"},
                },
                {
                    "occurred_at": "2026-08-29T09:00:00Z",
                    "values": {"observation": "today"},
                },
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        queried = call_tool(
            client,
            "query_ledger_entries",
            after="2026-08-29T00:00:00Z",
            ledger_id=ledger["id"],
        )

        assert_eq(
            [entry["values"] for entry in queried["result"]],
            [{"observation": "today"}],
        )


@test()
def ledger_query_filters_by_exact_field_value() -> None:
    """Dynamic field predicates remain scoped to one approved Ledger."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log clear sky and heavy rain.")
        _ = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {"values": {"observation": "clear sky"}},
                {"values": {"observation": "heavy rain"}},
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        queried = call_tool(
            client,
            "query_ledger_entries",
            field_equals={"observation": "clear sky"},
            ledger_id=ledger["id"],
        )

        assert_eq(
            [entry["values"] for entry in queried["result"]],
            [{"observation": "clear sky"}],
        )


@test()
def ledger_export_is_complete_and_byte_stable() -> None:
    """Export retains definitions, entries, Evidence, and supersession metadata."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log observation: clear sky.")
        entry = call_tool(
            client,
            "append_ledger_entries",
            entries=[{"values": {"observation": "clear sky"}}],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )["result"][0]

        first = client.get(f"/api/ledgers/{ledger['id']}/export")
        second = client.get(f"/api/ledgers/{ledger['id']}/export")

        assert_eq(first.status_code, 200)
        assert_eq(first.content, second.content)
        exported = first.json()
        assert_eq(exported["ledger_id"], ledger["id"])
        assert_eq([item["revision"] for item in exported["revisions"]], [1])
        assert_eq([item["id"] for item in exported["entries"]], [entry["id"]])
        assert_eq(len(exported["proposals"]), 1)


@test()
def proposal_cannot_be_approved_from_its_own_user_message() -> None:
    """The proposing turn cannot manufacture the required later approval."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_interactive_turn(client, "Propose an observation log.")
        proposal = call_tool(
            client,
            "propose_ledger",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                }
            ],
            name="Observation log",
            purpose="Record repeated observations.",
        )["result"]

        approval = call_tool(
            client,
            "approve_ledger_proposal",
            proposal_id=proposal["id"],
        )

        assert_eq(approval["success"], False)
        assert_eq(approval["error"]["code"], "invalid_input")
        assert_eq(client.get("/api/ledgers").json(), [])


@test()
def append_batch_is_atomic_when_one_entry_violates_the_schema() -> None:
    """One invalid member leaves every entry in the batch unrecorded."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log two observations.")

        appended = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {"values": {"observation": "clear sky"}},
                {"values": {"unknown": "heavy rain"}},
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        assert_eq(appended["success"], False)
        assert_eq(appended["error"]["code"], "invalid_input")
        assert_eq(client.get(f"/api/ledgers/{ledger['id']}/entries").json(), [])


@test()
def repeated_append_is_idempotent_for_the_same_evidence_and_content() -> None:
    """A repeated tool call returns the original entry instead of duplicating it."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log observation: clear sky.")
        params = {
            "entries": [{"values": {"observation": "clear sky"}}],
            "ledger_id": ledger["id"],
            "revision": ledger["revision"],
        }

        first = call_tool(client, "append_ledger_entries", **params)
        second = call_tool(client, "append_ledger_entries", **params)

        assert_eq(second["result"][0]["id"], first["result"][0]["id"])
        entries = client.get(f"/api/ledgers/{ledger['id']}/entries").json()
        assert_eq(len(entries), 1)


@test()
def identical_entries_in_one_batch_keep_distinct_record_identity() -> None:
    """Batch-position idempotency does not collapse two intentional records."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Log clear sky twice.")

        appended = call_tool(
            client,
            "append_ledger_entries",
            entries=[
                {"values": {"observation": "clear sky"}},
                {"values": {"observation": "clear sky"}},
            ],
            ledger_id=ledger["id"],
            revision=ledger["revision"],
        )

        assert_true(appended["success"])
        assert_eq(len({entry["id"] for entry in appended["result"]}), 2)


@test()
def revision_cannot_reinterpret_an_existing_field_identity() -> None:
    """Changing a stable field's type fails before a proposal is stored."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Change observation into an integer.")

        proposed = call_tool(
            client,
            "propose_ledger_revision",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "integer",
                }
            ],
            ledger_id=ledger["id"],
            name="Observation log",
            purpose="Record repeated observations.",
            revision=ledger["revision"],
            status="active",
        )

        assert_eq(proposed["success"], False)
        assert_eq(proposed["error"]["code"], "invalid_input")
        assert_eq(client.get("/api/ledger-proposals").json(), [])


@test()
def blank_ledger_field_metadata_is_rejected() -> None:
    """A schema cannot freeze field labels or descriptions without meaning."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        _begin_interactive_turn(client, "Propose an observation log.")

        proposed = call_tool(
            client,
            "propose_ledger",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "   ",
                    "required": True,
                    "type": "text",
                }
            ],
            name="Observation log",
            purpose="Record repeated observations.",
        )

        assert_eq(proposed["success"], False)
        assert_eq(proposed["error"]["code"], "invalid_input")
        assert_eq(client.get("/api/ledger-proposals").json(), [])


@test()
def unattended_run_cannot_propose_a_ledger() -> None:
    """Scheduled execution cannot impersonate fresh user approval authority."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        runtime = app_runtime(cast("Starlette", client.app))
        conversation_id = client.get("/api/conversations").json()[0]["id"]
        _ = runtime.trace_recorder.begin_run(
            session_id=SESSION,
            kind="scheduled",
            correlation=RunCorrelation(
                conversation_id=conversation_id,
                origin="scheduled",
                turn_id=str(uuid7()),
            ),
        )

        proposed = call_tool(
            client,
            "propose_ledger",
            fields=[
                {
                    "description": "The observed text.",
                    "field_id": "observation",
                    "label": "Observation",
                    "required": True,
                    "type": "text",
                }
            ],
            name="Observation log",
            purpose="Record repeated observations.",
        )

        assert_eq(proposed["success"], False)
        assert_eq(proposed["error"]["code"], "invalid_input")


@test()
def completed_ledger_rejects_new_entries() -> None:
    """An approved terminal lifecycle revision closes the append interface."""
    with TemporaryDirectory() as directory, surface_client(Path(directory)) as client:
        login(client)
        ledger = _create_approved_ledger(client)
        _begin_interactive_turn(client, "Complete that Ledger.")
        proposal = call_tool(
            client,
            "propose_ledger_revision",
            fields=ledger["fields"],
            ledger_id=ledger["id"],
            name=ledger["name"],
            purpose=ledger["purpose"],
            revision=ledger["revision"],
            status="completed",
        )["result"]
        _begin_interactive_turn(client, f"Approve Ledger proposal {proposal['id']}.")
        completed = call_tool(
            client,
            "approve_ledger_proposal",
            proposal_id=proposal["id"],
        )["result"]
        _begin_interactive_turn(client, "Log one more observation.")

        appended = call_tool(
            client,
            "append_ledger_entries",
            entries=[{"values": {"observation": "clear sky"}}],
            ledger_id=completed["id"],
            revision=completed["revision"],
        )

        assert_eq(appended["success"], False)
        assert_eq(appended["error"]["code"], "conflict")
