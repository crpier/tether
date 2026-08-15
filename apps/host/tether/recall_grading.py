"""Model-backed grading policy for free-text Recall answers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError
from snekok import Err, Ok, Result

_SHORT_ANSWER_GRADING_INSTRUCTIONS = """\
You are grading one short-answer recall response.

Question: {question}
Reference answer: {reference_answer}
Learner's answer: {answer_text}

The learner's answer is correct when it conveys the same fact as the reference
answer, allowing different wording. Return ONLY a JSON object (no prose, no
code fences) with this exact shape:
{{"correct": <true or false>}}
"""

_ESSAY_GRADING_INSTRUCTIONS = """\
You are proposing a grade for one essay recall response. A human will review
your proposal and make the final call, so explain your reasoning briefly.

Essay prompt: {question}
Rubric: {rubric}
Learner's essay: {answer_text}

The essay passes when it covers what the rubric requires. Return ONLY a JSON
object (no prose, no code fences) with this exact shape:
{{"correct": <true or false>, "reasoning": "<one or two sentences>"}}
"""


@dataclass(frozen=True, slots=True)
class AnswerGradingUnavailable:
    """An expected model reply that contains no trustworthy grading verdict."""

    message: str


class _MalformedGradingReplyError(Exception):
    """Identify a model reply that contains no recoverable JSON object."""


@dataclass(frozen=True, slots=True)
class EssayGradeProposal:
    """A model-proposed essay grade awaiting human confirmation."""

    correct: bool | None
    reasoning: str | None


@runtime_checkable
class AnswerGrader(Protocol):
    """Grade short answers and propose grades for essays."""

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> Result[bool, AnswerGradingUnavailable]:
        """Judge free text or return an unavailable-model outcome."""
        ...

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> Result[EssayGradeProposal, AnswerGradingUnavailable]:
        """Propose an essay grade or return an unavailable-model outcome."""
        ...


@runtime_checkable
class AgentTextRunner(Protocol):
    """Run a prompt through an agent and return its final text."""

    async def run(self, prompt: str) -> str:
        """Run one model prompt."""
        ...


class _ParsedShortAnswerGrade(BaseModel):
    """Parse a model's short-answer verdict."""

    correct: bool


class _ParsedEssayGrade(BaseModel):
    """Parse a model's essay-grade proposal."""

    correct: bool
    reasoning: str = ""


def _extract_json_object(text: str) -> str:
    """Recover the outermost JSON object from a model reply."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        message = "model reply contained no JSON object"
        raise _MalformedGradingReplyError(message)
    return text[start : end + 1]


def _normalized_answer(text: str) -> str:
    """Collapse whitespace and case for strict fallback matching."""
    return " ".join(text.split()).casefold()


def matches_reference(reference_answer: str, answer_text: str) -> bool:
    """Return whether free text exactly matches a reference after normalization.

    >>> matches_reference("epoll", "  EPOLL ")
    True
    """
    return _normalized_answer(reference_answer) == _normalized_answer(answer_text)


_UNUSABLE_GRADING_REPLY_ERRORS = (
    _MalformedGradingReplyError,
    ValidationError,
    json.JSONDecodeError,
)


class PiAnswerGrader:
    """Grade free-text answers through an agent with strict reply validation."""

    def __init__(self, runner: AgentTextRunner) -> None:
        self.runner: AgentTextRunner = runner

    async def grade_short_answer(
        self, *, question: str, reference_answer: str, answer_text: str
    ) -> Result[bool, AnswerGradingUnavailable]:
        """Judge a short answer or classify an unusable model reply."""
        reply = await self.runner.run(
            _SHORT_ANSWER_GRADING_INSTRUCTIONS.format(
                answer_text=answer_text,
                question=question,
                reference_answer=reference_answer,
            )
        )
        try:
            parsed = _ParsedShortAnswerGrade.model_validate_json(
                _extract_json_object(reply)
            )
        except _UNUSABLE_GRADING_REPLY_ERRORS as error:
            return Err(
                AnswerGradingUnavailable(
                    message=f"short-answer grading produced no verdict: {error}"
                )
            )
        return Ok(parsed.correct)

    async def propose_essay_grade(
        self, *, question: str, rubric: str, answer_text: str
    ) -> Result[EssayGradeProposal, AnswerGradingUnavailable]:
        """Propose an essay grade or classify an unusable model reply."""
        reply = await self.runner.run(
            _ESSAY_GRADING_INSTRUCTIONS.format(
                answer_text=answer_text,
                question=question,
                rubric=rubric,
            )
        )
        try:
            parsed = _ParsedEssayGrade.model_validate_json(_extract_json_object(reply))
        except _UNUSABLE_GRADING_REPLY_ERRORS as error:
            return Err(
                AnswerGradingUnavailable(
                    message=f"essay grading produced no proposal: {error}"
                )
            )
        return Ok(
            EssayGradeProposal(correct=parsed.correct, reasoning=parsed.reasoning)
        )
