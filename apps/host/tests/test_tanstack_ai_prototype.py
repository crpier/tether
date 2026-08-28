"""Throwaway TanStack AI protocol spike through authenticated host seams."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid7

from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def make_client(root: Path, *, faux_chat: bool = False) -> TestClient:
    """Create one isolated host for the prototype contract."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                default_model_id=(
                    "tether-chat-text-faux" if faux_chat else "tether-chat"
                ),
                default_model_provider="faux" if faux_chat else "openai-codex",
                extra_extension_paths=(
                    (
                        Path(__file__).resolve().parents[2]
                        / "agent/tests/fixtures/faux-chat-text.ts"
                    ),
                )
                if faux_chat
                else (),
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                tool_base_url="http://127.0.0.1:9",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


@test()
def agui_hydration_uses_tether_conversation_identity() -> None:
    """The prototype reads canonical history by TanStack thread id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login = client.post("/api/auth/login", json={"password": APP_PASSWORD})
        assert_eq(login.status_code, 204)
        created = client.post("/api/conversations", json={})
        conversation_id = created.json()["id"]

        response = client.get(
            "/api/prototypes/tanstack-ai/chat",
            params={"threadId": conversation_id},
        )

        assert_eq(response.status_code, 200)
        assert_eq(
            response.json(),
            {"activeRun": None, "interrupts": None, "messages": []},
        )


@test()
def agui_stream_completes_a_real_tether_turn() -> None:
    """A TanStack request reaches Pi and settles into canonical Messages."""
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), faux_chat=True) as client,
    ):
        login = client.post("/api/auth/login", json={"password": APP_PASSWORD})
        assert_eq(login.status_code, 204)
        created = client.post("/api/conversations", json={})
        conversation_id = created.json()["id"]
        run_id = str(uuid7())

        response = client.post(
            "/api/prototypes/tanstack-ai/chat",
            headers={"X-Run-Id": run_id},
            json={
                "context": [],
                "forwardedProps": {"replyMode": "text"},
                "messages": [
                    {
                        "content": "prototype hello",
                        "id": "optimistic-user",
                        "role": "user",
                    }
                ],
                "runId": run_id,
                "state": {},
                "threadId": conversation_id,
                "tools": [],
            },
        )

        assert_eq(response.status_code, 200)
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        event_types = [chunk["type"] for chunk in chunks]
        assert_eq(event_types[0], "RUN_STARTED")
        assert_eq(event_types[-1], "RUN_FINISHED")

        hydration = client.get(
            "/api/prototypes/tanstack-ai/chat",
            params={"threadId": conversation_id},
        ).json()
        assert_eq(
            [message["role"] for message in hydration["messages"]],
            [
                "user",
                "assistant",
            ],
        )
        assert_eq(
            "optimistic-user" in {message["id"] for message in hydration["messages"]},
            False,
        )
