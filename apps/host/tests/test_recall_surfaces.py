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
from tether.recall import EssayGradeProposal, GeneratedPrompt, GeneratedStudyItem
from tether.youtube import InMemoryYouTubeApi, RawYouTubeVideo


class FakeGenerator:
    """A controlled `StudyItemGenerator` returning a fixed distillation."""

    def __init__(self, distilled: GeneratedStudyItem) -> None:
        self.distilled: GeneratedStudyItem = distilled

    async def generate(self, *, transcript: str, title: str) -> GeneratedStudyItem:
        _ = (transcript, title)
        return self.distilled


class FakeGrader:
    """A controlled `AnswerGrader` with scripted verdicts."""

    def __init__(
        self, *, short_answer_correct: bool = True, proposal_correct: bool = True
    ) -> None:
        self.short_answer_correct: bool = short_answer_correct
        self.proposal_correct: bool = proposal_correct

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> bool:
        _ = (question, reference_answer, answer_text)
        return self.short_answer_correct

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> EssayGradeProposal:
        _ = (question, rubric, answer_text)
        return EssayGradeProposal(
            correct=self.proposal_correct, reasoning="Covers the rubric."
        )


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


def mixed_kind_distillation() -> GeneratedStudyItem:
    """A distillation with a short-answer and an essay prompt."""
    return GeneratedStudyItem(
        distilled_learnings="The event loop is built on epoll.",
        prompts=[
            GeneratedPrompt(
                question="Name the syscall behind the event loop.",
                kind="short_answer",
                reference_answer="epoll",
            ),
            GeneratedPrompt(
                question="Explain how an event loop schedules coroutines.",
                kind="essay",
                rubric="Mentions readiness, callbacks, and cooperative yielding.",
            ),
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


def make_client(
    root: Path,
    api: InMemoryYouTubeApi,
    distilled: GeneratedStudyItem | None = None,
    grader: FakeGrader | None = None,
) -> Any:
    """A dual-surface app with a seeded YouTube API and fake model seams."""
    return surface_client(
        root,
        youtube_api=api,
        study_item_generator=FakeGenerator(distilled or one_prompt_distillation()),
        answer_grader=grader or FakeGrader(),
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


# --- short-answer and essay prompts (#131) ---


@test()
def free_text_prompts_are_listed_without_their_grading_payloads() -> None:
    """A prompt read never leaks the reference answer or the rubric."""
    api = seeded_api()
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, distilled=mixed_kind_distillation()) as c,
    ):
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompts = due_prompts(c)

    kinds = {p["prompt"]["kind"] for p in prompts}
    assert_eq(kinds, {"short_answer", "essay"})
    for entry in prompts:
        assert_not_in("reference_answer", entry["prompt"])
        assert_not_in("rubric", entry["prompt"])
        assert_not_in("correct_index", entry["prompt"])


@test()
def answering_a_short_answer_prompt_with_free_text_grades_it() -> None:
    """`POST .../answer` accepts `answer_text` for a short-answer prompt."""
    api = seeded_api()
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, distilled=mixed_kind_distillation()) as c,
    ):
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompt_id = next(
            p["prompt"]["id"]
            for p in due_prompts(c)
            if p["prompt"]["kind"] == "short_answer"
        )

        response = c.post(
            f"/api/recall/prompts/{prompt_id}/answer",
            json={"answer_text": "it uses epoll", "response_ms": 1500},
        )

    assert_eq(response.status_code, 200)
    assert_true(response.json()["correct"])


@test()
def an_essay_flows_through_proposal_then_human_confirmed_answer() -> None:
    """The essay flow: propose a grade (rubric revealed), then confirm to answer."""
    api = seeded_api()
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, distilled=mixed_kind_distillation()) as c,
    ):
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompt_id = next(
            p["prompt"]["id"] for p in due_prompts(c) if p["prompt"]["kind"] == "essay"
        )

        proposed = c.post(
            f"/api/recall/prompts/{prompt_id}/grade-proposal",
            json={"answer_text": "Readiness polling plus cooperative yields."},
        )
        assert_eq(proposed.status_code, 200)
        proposal = proposed.json()

        answered = c.post(
            f"/api/recall/prompts/{prompt_id}/answer",
            json={
                "answer_text": "Readiness polling plus cooperative yields.",
                "confirmed_correct": False,
                "response_ms": 90_000,
            },
        )

    assert_eq(proposal["proposed_correct"], True)
    assert_eq(proposal["reasoning"], "Covers the rubric.")
    assert_eq(
        proposal["rubric"], "Mentions readiness, callbacks, and cooperative yielding."
    )
    # The human overrode the model's proposal; the recorded grade is theirs.
    assert_eq(answered.status_code, 200)
    assert_eq(answered.json()["correct"], False)


@test()
def an_essay_answer_without_confirmation_is_unprocessable() -> None:
    """An essay answer that skips the human-confirmed grade is rejected (422)."""
    api = seeded_api()
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, distilled=mixed_kind_distillation()) as c,
    ):
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompt_id = next(
            p["prompt"]["id"] for p in due_prompts(c) if p["prompt"]["kind"] == "essay"
        )

        response = c.post(
            f"/api/recall/prompts/{prompt_id}/answer",
            json={"answer_text": "An essay.", "response_ms": 1000},
        )

    assert_eq(response.status_code, 422)


@test()
def a_grade_proposal_for_a_multiple_choice_prompt_is_unprocessable() -> None:
    """Only essays carry a rubric to propose a grade against (422)."""
    api = seeded_api()
    with TemporaryDirectory() as directory, make_client(Path(directory), api) as c:
        login(c)
        ingest_with_transcript(c)
        _ = start_recall(c)
        prompt_id = due_prompts(c)[0]["prompt"]["id"]

        response = c.post(
            f"/api/recall/prompts/{prompt_id}/grade-proposal",
            json={"answer_text": "An answer."},
        )

    assert_eq(response.status_code, 422)


@test()
def the_recall_tools_drive_the_free_text_flow() -> None:
    """`answer_recall_prompt` takes free text and `propose_essay_grade` proposes."""
    api = seeded_api()
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), api, distilled=mixed_kind_distillation()) as c,
    ):
        _ = call_tool(c, "browse_youtube")
        _ = call_tool(c, "fetch_youtube_transcript", video_id="v1")
        _ = call_tool(c, "start_recall", video_id="v1")
        due = call_tool(c, "list_due_recall_prompts")["result"]
        by_kind = {entry["prompt"]["kind"]: entry["prompt"] for entry in due}

        short = call_tool(
            c,
            "answer_recall_prompt",
            prompt_id=by_kind["short_answer"]["id"],
            answer_text="epoll",
            response_ms=1200,
        )
        proposed = call_tool(
            c,
            "propose_essay_grade",
            prompt_id=by_kind["essay"]["id"],
            answer_text="Readiness polling plus cooperative yields.",
        )
        essay = call_tool(
            c,
            "answer_recall_prompt",
            prompt_id=by_kind["essay"]["id"],
            answer_text="Readiness polling plus cooperative yields.",
            confirmed_correct=True,
            response_ms=60_000,
        )

    assert_true(short["success"])
    assert_true(short["result"]["correct"])
    assert_true(proposed["success"])
    assert_eq(proposed["result"]["proposed_correct"], True)
    assert_true(essay["success"])
    assert_true(essay["result"]["correct"])


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
