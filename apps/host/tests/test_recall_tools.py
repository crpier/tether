"""Behavior tests for the loopback internal Recall tool surface.

These drive the mounted Starlette app through `TestClient`, calling the
`/internal/tools/*` Recall endpoints directly — no LLM, no pi, no live YouTube.
The app is wired with a seeded `InMemoryYouTubeApi` (so a source exists to
distil) and an injected fake study-item generator (so distillation is
deterministic). Beyond the shared auth gate and envelope, these assert the
Recall-specific behavior: starting Recall on a transcribed source, listing due
prompts without leaking the answer key, and grading an answer.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_not_in, assert_true, test
from starlette.testclient import TestClient

from tether.recall import GeneratedPrompt, GeneratedStudyItem
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


class FakeGenerator:
    """A controlled `StudyItemGenerator` returning a fixed distillation."""

    def __init__(self, distilled: GeneratedStudyItem) -> None:
        self.distilled: GeneratedStudyItem = distilled

    async def generate(self, *, transcript: str, title: str) -> GeneratedStudyItem:
        _ = (transcript, title)
        return self.distilled


def one_prompt_distillation() -> GeneratedStudyItem:
    """A distillation with a single multiple-choice prompt."""
    return GeneratedStudyItem(
        distilled_learnings="Async IO multiplexes one thread over many waits.",
        prompts=[
            GeneratedPrompt(
                question="What does async IO multiplex?",
                choices=["One thread", "Many threads", "Processes"],
                correct_index=0,
            )
        ],
    )


def seeded_api() -> InMemoryYouTubeApi:
    """A YouTube API holding one liked video with a transcript."""
    return InMemoryYouTubeApi(
        liked=[
            RawYouTubeVideo(
                video_id="v1", title="Async IO", channel="PyConf", topic="python"
            )
        ],
        transcripts={"v1": "Async IO multiplexes one thread over many awaited waits."},
    )


def make_client(root: Path, api: InMemoryYouTubeApi) -> TestClient:
    """A test app with a seeded YouTube API and an injected fake generator."""
    app = create_app(
        config=AppConfig(
            app_password="test-app-password",
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret="test-session-secret",
            youtube_api=api,
            study_item_generator=FakeGenerator(one_prompt_distillation()),
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        tool_secret=SECRET,
    )
    cast("SessionRegistry", app.state.session_registry).register(SESSION)
    return TestClient(app)


def call(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke a tool with the known secret and session, returning the envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


def ingest_with_transcript(client: TestClient) -> None:
    """Mirror the seeded source and fetch its transcript through the YouTube tools."""
    _ = call(client, "browse_youtube")
    _ = call(client, "fetch_youtube_transcript", video_id="v1")


@test()
def start_recall_distils_a_transcribed_source_into_a_study_item() -> None:
    """`start_recall` returns a studying study item for a transcribed source."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        ingest_with_transcript(client)
        envelope = call(client, "start_recall", video_id="v1")

    assert_true(envelope["success"])
    assert_eq(envelope["result"]["state"], "studying")
    assert_eq(envelope["result"]["source_video_id"], "v1")


@test()
def start_recall_without_a_transcript_fails_with_invalid_input() -> None:
    """Starting Recall on a source with no transcript is a clean envelope failure."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        _ = call(client, "browse_youtube")  # mirror only, no transcript fetched
        envelope = call(client, "start_recall", video_id="v1")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def listing_due_prompts_omits_the_answer_key() -> None:
    """`list_due_recall_prompts` exposes choices but never `correct_index`."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        ingest_with_transcript(client)
        _ = call(client, "start_recall", video_id="v1")
        envelope = call(client, "list_due_recall_prompts")

    assert_true(envelope["success"])
    assert_eq(len(envelope["result"]), 1)
    prompt = envelope["result"][0]["prompt"]
    assert_not_in("correct_index", prompt)


@test()
def answering_a_prompt_grades_it() -> None:
    """`answer_recall_prompt` grades the chosen option and returns the outcome."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        ingest_with_transcript(client)
        _ = call(client, "start_recall", video_id="v1")
        prompt_id = call(client, "list_due_recall_prompts")["result"][0]["prompt"]["id"]

        envelope = call(
            client,
            "answer_recall_prompt",
            prompt_id=prompt_id,
            selected_index=0,
            response_ms=1200,
        )

    assert_true(envelope["success"])
    assert_true(envelope["result"]["correct"])
    assert_eq(envelope["result"]["completed"], False)


@test()
def answering_an_unknown_prompt_is_not_found() -> None:
    """Answering a non-existent prompt yields a `not_found` envelope."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        envelope = call(
            client,
            "answer_recall_prompt",
            prompt_id="018f0000-0000-7000-8000-000000000000",
            selected_index=0,
            response_ms=1000,
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "not_found")


@test()
def the_tool_gate_rejects_a_missing_secret() -> None:
    """A Recall tool call without the process secret is a hard 401, not an envelope."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as client:
        response = client.post(
            "/internal/tools/list_due_recall_prompts",
            json={"session_id": SESSION},
        )

    assert_eq(response.status_code, 401)
