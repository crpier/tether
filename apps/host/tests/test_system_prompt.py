"""Tether system prompt selection per agent run kind."""

from typing import get_args

from snektest import assert_eq, assert_in, test

from tether.agent_trace import RunKind
from tether.system_prompt import (
    CONVERSATION_SYSTEM_PROMPT,
    TASK_SYSTEM_PROMPT,
    system_prompt_for,
)


@test()
async def conversation_runs_get_the_full_persona_prompt() -> None:
    """The interactive chat run kind maps to the full conversation prompt."""
    assert_eq(system_prompt_for("conversation"), CONVERSATION_SYSTEM_PROMPT)


@test()
async def scheduled_runs_get_the_short_task_prompt() -> None:
    """Scheduled-trigger runs map to the shorter unattended-task prompt."""
    assert_eq(system_prompt_for("scheduled"), TASK_SYSTEM_PROMPT)


@test()
async def recall_runs_get_the_short_task_prompt() -> None:
    """Recall model steps map to the shorter unattended-task prompt."""
    assert_eq(system_prompt_for("recall"), TASK_SYSTEM_PROMPT)


@test()
async def every_run_kind_resolves_to_a_tether_prompt() -> None:
    """No RunKind member falls through system_prompt_for unmapped."""
    for kind in get_args(RunKind.__value__):
        assert_in("Tether", system_prompt_for(kind))


@test()
async def both_prompts_state_the_tether_identity() -> None:
    """Every run kind's prompt names the Tether persona."""
    for prompt in (CONVERSATION_SYSTEM_PROMPT, TASK_SYSTEM_PROMPT):
        assert_in("Tether", prompt)


@test()
async def both_prompts_state_the_trust_contract() -> None:
    """Every run kind's prompt carries the loose/tethered trust vocabulary."""
    for prompt in (CONVERSATION_SYSTEM_PROMPT, TASK_SYSTEM_PROMPT):
        assert_in("loose", prompt)
        assert_in("tethered", prompt)


@test()
async def the_conversation_prompt_allows_executing_a_human_tether_decision() -> None:
    """The trust contract names `tether`/`reject` as the execution path."""
    assert_in("`tether`", CONVERSATION_SYSTEM_PROMPT)
    assert_in("`reject`", CONVERSATION_SYSTEM_PROMPT)


@test()
async def the_conversation_prompt_covers_capture_discipline() -> None:
    """The full prompt demands the human's own intent context for captures."""
    assert_in("intent context", CONVERSATION_SYSTEM_PROMPT)


@test()
async def the_conversation_prompt_attributes_dedup_to_the_add_tools() -> None:
    """Duplicate flagging belongs to the `add_*` tools, not search."""
    assert_in("`add_*`", CONVERSATION_SYSTEM_PROMPT)


@test()
async def the_conversation_prompt_covers_the_tool_belt() -> None:
    """The full prompt names the review-digest and triage tools."""
    assert_in("review_digest", CONVERSATION_SYSTEM_PROMPT)
    assert_in("triage_report", CONVERSATION_SYSTEM_PROMPT)


@test()
async def the_conversation_prompt_speaks_recall_vocabulary() -> None:
    """The full prompt uses the recall-prompt vocabulary."""
    assert_in("recall prompt", CONVERSATION_SYSTEM_PROMPT)
