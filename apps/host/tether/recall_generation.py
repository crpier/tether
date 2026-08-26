"""Model-backed generation of Recall study items."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError
from snekok import Err, Ok, Result

from tether.recall_schedule import RecallPromptKind

_MIN_CHOICES = 2
_DEFAULT_PROMPT_COUNT = 5

_GENERATION_INSTRUCTIONS = """\
You are distilling an educational video transcript into a compact study item for \
spaced-repetition recall.

Return ONLY a JSON object (no prose, no code fences) with this exact shape:
{{
  "distilled_learnings": "<a few sentences capturing the key learnings>",
  "prompts": [
    {{
      "kind": "multiple_choice",
      "question": "<a multiple-choice question testing one learning>",
      "choices": ["<option 0>", "<option 1>", "<option 2>", "<option 3>"],
      "correct_index": <0-based index of the correct choice>
    }},
    {{
      "kind": "short_answer",
      "question": "<a question answered in a word or short phrase>",
      "reference_answer": "<the expected answer, used to grade free text>"
    }},
    {{
      "kind": "essay",
      "question": "<a prompt asking the learner to explain a learning in depth>",
      "rubric": "<what a strong answer must cover, used to grade the essay>"
    }}
  ]
}}

Produce {count} prompts, mixing the kinds: mostly multiple_choice and \
short_answer, and at most one essay. Each multiple_choice prompt must have at \
least two choices and exactly one correct answer. Base everything strictly on \
the transcript.

Title: {title}

Transcript:
{transcript}
"""


class _MalformedGenerationReplyError(Exception):
    """Identify a model reply that contains no recoverable JSON object."""


@dataclass(frozen=True, slots=True)
class StudyItemGenerationFailure:
    """An expected malformed or invalid model-generated study item."""

    message: str


@dataclass(frozen=True, slots=True)
class GeneratedPrompt:
    """One generated prompt and the grading payload selected by its kind."""

    question: str
    choices: list[str] = field(default_factory=list[str])
    correct_index: int | None = None
    kind: RecallPromptKind = "multiple_choice"
    reference_answer: str | None = None
    rubric: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedStudyItem:
    """A source's distilled learnings and generated Recall prompts."""

    distilled_learnings: str
    prompts: list[GeneratedPrompt]


@runtime_checkable
class StudyItemGenerator(Protocol):
    """Distil a transcript into learnings and Recall prompts."""

    async def generate(
        self, *, transcript: str, title: str
    ) -> Result[GeneratedStudyItem, StudyItemGenerationFailure]:
        """Produce a generated study item or an expected reply failure."""
        ...


@runtime_checkable
class AgentTextRunner(Protocol):
    """Run a prompt through an agent and return its final text."""

    async def run(self, prompt: str) -> str:
        """Run one model prompt."""
        ...


class _ParsedPrompt(BaseModel):
    """Parse one model-produced prompt before domain validation."""

    choices: list[str] = Field(default_factory=list[str])
    correct_index: int | None = None
    kind: RecallPromptKind = "multiple_choice"
    question: str
    reference_answer: str | None = None
    rubric: str | None = None


class _ParsedStudyItem(BaseModel):
    """Parse the model's study-item JSON."""

    distilled_learnings: str
    prompts: list[_ParsedPrompt]


def _extract_json_object(text: str) -> str:
    """Recover the outermost JSON object from a model reply."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        message = "model reply contained no JSON object"
        raise _MalformedGenerationReplyError(message)
    return text[start : end + 1]


def _validate_multiple_choice(
    prompt: GeneratedPrompt,
) -> StudyItemGenerationFailure | None:
    """Return the choice-prompt failure without throwing expected invalid input."""
    if len(prompt.choices) < _MIN_CHOICES:
        return StudyItemGenerationFailure(
            message="a multiple-choice prompt requires at least two choices"
        )
    if prompt.correct_index is None or not 0 <= prompt.correct_index < len(
        prompt.choices
    ):
        return StudyItemGenerationFailure(
            message=(
                f"correct_index {prompt.correct_index} is outside the "
                f"{len(prompt.choices)} choices"
            )
        )
    return None


def validate_generated_study_item(
    generated: GeneratedStudyItem,
) -> Result[None, StudyItemGenerationFailure]:
    """Validate generated prompt payloads as an expected result."""
    if not generated.prompts:
        return Err(
            StudyItemGenerationFailure(
                message="a study item requires at least one recall prompt"
            )
        )
    for prompt in generated.prompts:
        kind: str = prompt.kind
        if kind == "multiple_choice":
            if failure := _validate_multiple_choice(prompt):
                return Err(failure)
        elif kind == "short_answer":
            if not (prompt.reference_answer or "").strip():
                return Err(
                    StudyItemGenerationFailure(
                        message="a short-answer prompt requires a reference answer"
                    )
                )
        elif kind == "essay":
            if not (prompt.rubric or "").strip():
                return Err(
                    StudyItemGenerationFailure(
                        message="an essay prompt requires a rubric"
                    )
                )
        else:
            return Err(
                StudyItemGenerationFailure(
                    message=f"unknown recall prompt kind {kind!r}"
                )
            )
    return Ok(None)


class PiStudyItemGenerator:
    """Distil transcripts through an agent while validating its JSON reply."""

    def __init__(
        self, runner: AgentTextRunner, *, prompt_count: int = _DEFAULT_PROMPT_COUNT
    ) -> None:
        self.runner: AgentTextRunner = runner
        self.prompt_count: int = prompt_count

    async def generate(
        self, *, transcript: str, title: str
    ) -> Result[GeneratedStudyItem, StudyItemGenerationFailure]:
        """Distil a transcript or classify its malformed model reply."""
        reply = await self.runner.run(
            _GENERATION_INSTRUCTIONS.format(
                count=self.prompt_count,
                title=title,
                transcript=transcript,
            )
        )
        try:
            parsed = _ParsedStudyItem.model_validate_json(_extract_json_object(reply))
        except (
            _MalformedGenerationReplyError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            return Err(
                StudyItemGenerationFailure(
                    message=f"model produced an unusable study item: {error}"
                )
            )
        generated = GeneratedStudyItem(
            distilled_learnings=parsed.distilled_learnings,
            prompts=[
                GeneratedPrompt(
                    choices=prompt.choices,
                    correct_index=prompt.correct_index,
                    kind=prompt.kind,
                    question=prompt.question,
                    reference_answer=prompt.reference_answer,
                    rubric=prompt.rubric,
                )
                for prompt in parsed.prompts
            ],
        )
        validation = validate_generated_study_item(generated)
        if isinstance(validation, Err):
            return Err(validation.error)
        return Ok(generated)
