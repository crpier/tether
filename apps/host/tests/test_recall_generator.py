"""Behavior tests for the live study-item generator's reply parsing.

`PiStudyItemGenerator` is the one model-backed step in Recall. These tests drive
it with a fake runner that returns canned text — no pi, no model — to prove it
recovers the JSON object from a reply (even wrapped in prose or code fences) and
that a malformed reply degrades to a clean `InvalidPromptError` rather than a
corrupt study item.
"""

from snektest import assert_eq, assert_raises, test

from tether.recall import (
    InvalidPromptError,
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
