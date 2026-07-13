"""Behavior tests for the live study-item generator's reply parsing.

`PiStudyItemGenerator` is the one model-backed step in Recall. These tests drive
it with a fake runner that returns canned text — no pi, no model — to prove it
recovers the JSON object from a reply (even wrapped in prose or code fences) and
that a malformed reply degrades to a clean `InvalidPromptError` rather than a
corrupt study item.
"""

from snektest import assert_eq, assert_raises, test

from tether.recall import (
    AnswerGradingUnavailableError,
    InvalidPromptError,
    PiAnswerGrader,
    PiStudyItemGenerator,
)

VALID_JSON = """\
{
  "distilled_learnings": "Async IO multiplexes one thread over many waits.",
  "prompts": [
    {"question": "What does async IO multiplex?",
     "choices": ["One thread", "Many threads"],
     "correct_index": 0}
  ]
}
"""


class FakeRunner:
    """An `AgentTextRunner` that returns a fixed reply and records the prompt."""

    def __init__(self, reply: str) -> None:
        self.reply: str = reply
        self.prompt: str | None = None

    async def run(self, prompt: str) -> str:
        self.prompt = prompt
        return self.reply


@test()
async def parses_a_clean_json_reply_into_a_study_item() -> None:
    """A well-formed JSON reply becomes distilled learnings plus prompts."""
    generator = PiStudyItemGenerator(FakeRunner(VALID_JSON))

    generated = await generator.generate(transcript="t", title="Async IO")

    assert_eq(
        generated.distilled_learnings,
        "Async IO multiplexes one thread over many waits.",
    )
    assert_eq(len(generated.prompts), 1)
    assert_eq(generated.prompts[0].correct_index, 0)


@test()
async def recovers_json_wrapped_in_prose_and_code_fences() -> None:
    """A reply that buries the JSON in commentary or fences still parses."""
    noisy = f"Sure! Here is the study item:\n```json\n{VALID_JSON}\n```\nHope it helps."
    generator = PiStudyItemGenerator(FakeRunner(noisy))

    generated = await generator.generate(transcript="t", title="Async IO")

    assert_eq(len(generated.prompts), 1)


@test()
async def a_reply_without_json_is_an_invalid_prompt_error() -> None:
    """A reply with no JSON object fails cleanly, producing no study item."""
    generator = PiStudyItemGenerator(FakeRunner("I could not produce questions."))

    with assert_raises(InvalidPromptError):
        _ = await generator.generate(transcript="t", title="Async IO")


@test()
async def the_prompt_carries_the_transcript_and_title() -> None:
    """The generator hands the model the transcript and title to distil."""
    runner = FakeRunner(VALID_JSON)
    generator = PiStudyItemGenerator(runner)

    _ = await generator.generate(transcript="THE-TRANSCRIPT", title="THE-TITLE")

    assert runner.prompt is not None
    assert_eq("THE-TRANSCRIPT" in runner.prompt, True)
    assert_eq("THE-TITLE" in runner.prompt, True)


# --- prompt kinds (#131) ---

MIXED_JSON = """\
{
  "distilled_learnings": "Async IO multiplexes one thread over many waits.",
  "prompts": [
    {"kind": "multiple_choice",
     "question": "What does async IO multiplex?",
     "choices": ["One thread", "Many threads"],
     "correct_index": 0},
    {"kind": "short_answer",
     "question": "Name the syscall behind the event loop.",
     "reference_answer": "epoll"},
    {"kind": "essay",
     "question": "Explain how an event loop schedules coroutines.",
     "rubric": "Mentions readiness, callbacks, and cooperative yielding."}
  ]
}
"""


@test()
async def parses_a_mix_of_prompt_kinds() -> None:
    """Short-answer and essay prompts parse with their grading payloads."""
    generator = PiStudyItemGenerator(FakeRunner(MIXED_JSON))

    generated = await generator.generate(transcript="t", title="Async IO")

    kinds = [prompt.kind for prompt in generated.prompts]
    assert_eq(kinds, ["multiple_choice", "short_answer", "essay"])
    assert_eq(generated.prompts[1].reference_answer, "epoll")
    assert_eq(
        generated.prompts[2].rubric,
        "Mentions readiness, callbacks, and cooperative yielding.",
    )


@test()
async def a_prompt_without_a_kind_defaults_to_multiple_choice() -> None:
    """Legacy replies without a `kind` field stay multiple-choice."""
    generator = PiStudyItemGenerator(FakeRunner(VALID_JSON))

    generated = await generator.generate(transcript="t", title="Async IO")

    assert_eq(generated.prompts[0].kind, "multiple_choice")


@test()
async def the_generation_prompt_asks_for_a_mix_of_kinds() -> None:
    """The instructions describe all three prompt kinds so the model mixes them."""
    runner = FakeRunner(MIXED_JSON)
    generator = PiStudyItemGenerator(runner)

    _ = await generator.generate(transcript="t", title="Async IO")

    assert runner.prompt is not None
    assert_eq("short_answer" in runner.prompt, True)
    assert_eq("essay" in runner.prompt, True)
    assert_eq("rubric" in runner.prompt, True)


# --- the model-backed answer grader (#131) ---


@test()
async def grades_a_short_answer_from_the_model_verdict() -> None:
    """The grader turns the model's JSON verdict into a boolean grade."""
    runner = FakeRunner('{"correct": true}')
    grader = PiAnswerGrader(runner)

    correct = await grader.grade_short_answer(
        question="Name the syscall.",
        reference_answer="epoll",
        answer_text="it uses epoll",
    )

    assert_eq(correct, True)
    assert runner.prompt is not None
    assert_eq("Name the syscall." in runner.prompt, True)
    assert_eq("epoll" in runner.prompt, True)
    assert_eq("it uses epoll" in runner.prompt, True)


@test()
async def a_negative_model_verdict_grades_incorrect() -> None:
    """A `correct: false` verdict comes back as an incorrect grade."""
    grader = PiAnswerGrader(FakeRunner('{"correct": false}'))

    correct = await grader.grade_short_answer(
        question="q", reference_answer="epoll", answer_text="select"
    )

    assert_eq(correct, False)


@test()
async def an_unusable_grading_reply_is_a_grading_unavailable_error() -> None:
    """A reply with no verdict fails as unavailable so callers can fall back."""
    grader = PiAnswerGrader(FakeRunner("I cannot grade this."))

    with assert_raises(AnswerGradingUnavailableError):
        _ = await grader.grade_short_answer(
            question="q", reference_answer="epoll", answer_text="epoll"
        )


class BrokenRunner:
    """An `AgentTextRunner` whose model call always fails."""

    async def run(self, prompt: str) -> str:
        _ = prompt
        message = "pi is down"
        raise RuntimeError(message)


@test()
async def a_failing_model_call_propagates_as_a_real_error() -> None:
    """A runner failure is a genuine error, not a silent fallback grade.

    Only an unparseable reply degrades to `AnswerGradingUnavailableError`; a
    failing run (or any bug in the grading path) must surface so it cannot
    quietly downgrade every answer to the strict-match fallback.
    """
    grader = PiAnswerGrader(BrokenRunner())

    with assert_raises(RuntimeError):
        _ = await grader.grade_short_answer(
            question="q", reference_answer="epoll", answer_text="epoll"
        )

    with assert_raises(RuntimeError):
        _ = await grader.propose_essay_grade(question="q", rubric="r", answer_text="a")


@test()
async def proposes_an_essay_grade_against_the_rubric() -> None:
    """The grader returns the model's proposed grade and reasoning for an essay."""
    runner = FakeRunner('{"correct": true, "reasoning": "Covers all rubric points."}')
    grader = PiAnswerGrader(runner)

    proposal = await grader.propose_essay_grade(
        question="Explain the event loop.",
        rubric="Mentions readiness and yielding.",
        answer_text="The loop polls readiness and coroutines yield cooperatively.",
    )

    assert_eq(proposal.correct, True)
    assert_eq(proposal.reasoning, "Covers all rubric points.")
    assert runner.prompt is not None
    assert_eq("Mentions readiness and yielding." in runner.prompt, True)


@test()
async def an_unusable_essay_proposal_is_a_grading_unavailable_error() -> None:
    """An essay proposal the model fumbles degrades to unavailable, not a grade."""
    grader = PiAnswerGrader(FakeRunner("no json here"))

    with assert_raises(AnswerGradingUnavailableError):
        _ = await grader.propose_essay_grade(question="q", rubric="r", answer_text="a")
