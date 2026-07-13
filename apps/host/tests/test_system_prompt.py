"""Tether system prompt selection per agent run kind."""

from snektest import assert_eq, assert_in, test

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
async def both_prompts_state_the_tether_identity_and_trust_contract() -> None:
    """Every run kind's prompt names Tether and the loose/tethered contract."""
    for prompt in (CONVERSATION_SYSTEM_PROMPT, TASK_SYSTEM_PROMPT):
        assert_in("Tether", prompt)
        assert_in("loose", prompt)
        assert_in("tethered", prompt)


@test()
async def the_conversation_prompt_covers_capture_discipline_and_tools() -> None:
    """The full prompt covers intent context, junk-capture rules, and tools."""
    assert_in("intent context", CONVERSATION_SYSTEM_PROMPT)
    assert_in("review_digest", CONVERSATION_SYSTEM_PROMPT)
    assert_in("triage_report", CONVERSATION_SYSTEM_PROMPT)
    assert_in("recall prompt", CONVERSATION_SYSTEM_PROMPT)
