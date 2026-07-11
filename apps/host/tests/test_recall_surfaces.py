"""Dual-surface behaviour tests for the Recall tethering path.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation; the `/internal/tools/*` endpoints assert the
uniform envelope. Both derive from `tether.recall_capabilities`, with a seeded
`InMemoryYouTubeApi` for the source video and an injected fake study-item
generator, so no live YouTube call or model run ever happens.

The recall surface must never leak an answer key: a prompt read carries its
`choices` but not `correct_index`, and these tests assert that on both shells.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_not_in, assert_true, test
from starlette.testclient import TestClient

from tests.surfaces import SESSION, call_tool, login, surface_client
from tether.recall import GeneratedPrompt, GeneratedStudyItem
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo


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
                video_id="v1",
                title="Async IO Explained",
                channel="PyConf",
                topic="python",
            )
        ],
        transcripts={"v1": "Async IO multiplexes one thread over many awaited waits."},
    )


def make_client(root: Path, api: InMemoryYouTubeApi) -> Any:
    """A dual-surface app with a seeded YouTube API and a fake generator."""
    return surface_client(
        root,
        youtube_api=api,
        study_item_generator=FakeGenerator(one_prompt_distillation()),
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
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
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
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        login(c)
        assert_eq(c.get("/api/youtube").status_code, 200)  # mirror, but no transcript
        response = c.post("/api/recall/study-items", json={"video_id": "v1"})

    assert_eq(response.status_code, 422)


@test()
def starting_recall_twice_for_one_source_conflicts() -> None:
    """A source becomes a study item at most once (409)."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        response = c.post("/api/recall/study-items", json={"video_id": "v1"})

    assert_eq(response.status_code, 409)


@test()
def due_prompts_are_listed_without_the_answer_key() -> None:
    """`GET /api/recall/prompts` lists outstanding prompts but hides `correct_index`."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
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
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
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


@test()
def the_recall_tool_belt_drives_the_full_flow() -> None:
    """start, list-due, and answer all work through the tool seam's envelopes.

    One flow exercises every Recall tool binding: the study item lands
    `studying`, the due prompt hides `correct_index`, and answering grades the
    chosen option.
    """
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        _ = call_tool(c, "browse_youtube")
        _ = call_tool(c, "fetch_youtube_transcript", video_id="v1")

        started = call_tool(c, "start_recall", video_id="v1")
        assert_true(started["success"])
        assert_eq(started["result"]["state"], "studying")
        assert_eq(started["result"]["source_video_id"], "v1")

        due = call_tool(c, "list_due_recall_prompts")
        assert_true(due["success"])
        assert_eq(len(due["result"]), 1)
        prompt = due["result"][0]["prompt"]
        assert_not_in("correct_index", prompt)

        answered = call_tool(
            c,
            "answer_recall_prompt",
            prompt_id=prompt["id"],
            selected_index=0,
            response_ms=1200,
        )

    assert_true(answered["success"])
    assert_true(answered["result"]["correct"])
    assert_eq(answered["result"]["completed"], False)


@test()
def start_recall_without_a_transcript_fails_with_invalid_input() -> None:
    """Starting Recall on a source with no transcript is a clean envelope failure."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        _ = call_tool(c, "browse_youtube")  # mirror only, no transcript fetched
        envelope = call_tool(c, "start_recall", video_id="v1")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def answering_an_unknown_prompt_is_not_found() -> None:
    """Answering a non-existent prompt yields a `not_found` envelope."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        envelope = call_tool(
            c,
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
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        response = c.post(
            "/internal/tools/list_due_recall_prompts",
            json={"session_id": SESSION},
        )

    assert_eq(response.status_code, 401)
