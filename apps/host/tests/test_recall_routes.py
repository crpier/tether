"""REST behavior tests for the Recall surface.

These drive the mounted Starlette app through `TestClient` — request parsing,
route wiring, service behavior, and serialization together — with a seeded
`InMemoryYouTubeApi` for the source video and an injected fake study-item
generator, so no live YouTube call or model run ever happens. The browser
surface is authenticated, so each test logs in first.

The recall surface must never leak an answer key: a prompt read carries its
`choices` but not `correct_index`, and these tests assert that.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_not_in, assert_true, test
from starlette.testclient import TestClient

from tether.recall import GeneratedPrompt, GeneratedStudyItem
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


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


def make_client(
    root: Path,
    *,
    api: InMemoryYouTubeApi,
    distillation: GeneratedStudyItem | None = None,
) -> TestClient:
    """A test app with a seeded YouTube API and an injected fake generator."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
                youtube_api=api,
                study_item_generator=FakeGenerator(
                    distillation or one_prompt_distillation()
                ),
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def seeded_api() -> InMemoryYouTubeApi:
    """A YouTube API holding one liked video with a transcript."""
    return InMemoryYouTubeApi(
        liked=[
            RawYouTubeVideo(
                video_id="v1",
                title="Async IO Explained",
                channel="PyConf",
                topic="python",
            )
        ],
        transcripts={"v1": "Async IO multiplexes one thread over many awaited waits."},
    )


def ingest_with_transcript(client: TestClient, video_id: str = "v1") -> None:
    """Browse (to mirror the video) then fetch its transcript so Recall can start."""
    assert_eq(client.get("/api/youtube").status_code, 200)
    response = client.post(f"/api/youtube/{video_id}/transcript")
    assert_eq(response.status_code, 200)


def start_recall(client: TestClient, video_id: str = "v1") -> dict[str, Any]:
    """Start Recall for an ingested video and return the study-item JSON."""
    response = client.post("/api/recall/study-items", json={"video_id": video_id})
    assert_eq(response.status_code, 201)
    return response.json()


def due_prompts(client: TestClient) -> list[dict[str, Any]]:
    """Fetch the outstanding recall prompts."""
    response = client.get("/api/recall/prompts")
    assert_eq(response.status_code, 200)
    return response.json()


@test()
def starting_recall_creates_a_studying_item_from_a_transcribed_video() -> None:
    """`POST /api/recall/study-items` distils a transcribed video into a study item."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api=api) as c:
        login(c)
        ingest_with_transcript(c)
        item = start_recall(c)

    assert_eq(item["state"], "studying")
    assert_eq(item["source_video_id"], "v1")
    assert_eq(item["source_title"], "Async IO Explained")
    assert_eq(item["completed_at"], None)


@test()
def starting_recall_without_a_transcript_is_unprocessable() -> None:
    """A source whose transcript was never fetched cannot be distilled (422)."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api=api) as c:
        login(c)
        assert_eq(c.get("/api/youtube").status_code, 200)  # mirror, but no transcript
        response = c.post("/api/recall/study-items", json={"video_id": "v1"})

    assert_eq(response.status_code, 422)


@test()
def starting_recall_twice_for_one_source_conflicts() -> None:
    """A source becomes a study item at most once (409)."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api=api) as c:
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        response = c.post("/api/recall/study-items", json={"video_id": "v1"})

    assert_eq(response.status_code, 409)


@test()
def due_prompts_are_listed_without_the_answer_key() -> None:
    """`GET /api/recall/prompts` lists outstanding prompts but hides `correct_index`."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api=api) as c:
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompts = due_prompts(c)

    assert_eq(len(prompts), 1)
    prompt = prompts[0]["prompt"]
    assert_eq(prompt["question"], "What does async IO multiplex?")
    assert_eq(len(prompt["choices"]), 3)
    assert_not_in("correct_index", prompt)


@test()
def answering_correctly_grades_and_reschedules_the_prompt() -> None:
    """`POST .../answer` grades the choice and pushes the prompt off the due list."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api=api) as c:
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompt_id = due_prompts(c)[0]["prompt"]["id"]

        response = c.post(
            f"/api/recall/prompts/{prompt_id}/answer",
            json={"selected_index": 0, "response_ms": 1500},
        )
        assert_eq(response.status_code, 200)
        outcome = response.json()
        remaining = due_prompts(c)

    assert_true(outcome["correct"])
    assert_eq(outcome["completed"], False)
    assert_eq(len(remaining), 0)
