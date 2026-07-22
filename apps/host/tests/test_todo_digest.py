"""Tests for the standing Todo digest composition (todos in -> block out).

The digest is a pure seam: a `TodoReadiness` in, a system-prompt block out. These
assert on the rendered text at that boundary — never by inspecting a live prompt.
"""

from datetime import UTC, datetime
from uuid import uuid7

from snekql.sqlite import Fetched
from snektest import assert_eq, assert_in, assert_not_in, assert_true, test

from tether.system_prompt import CONVERSATION_SYSTEM_PROMPT, compose_conversation_prompt
from tether.todo_digest import render_todo_digest
from tether.todos import Todo, TodoReadiness


def _todo(action: str, *, condition: str | None = None) -> Todo[Fetched]:
    """A detached Todo carrying just the fields the digest reads."""
    return Todo.construct(  # type: ignore[return-value]
        id=uuid7(),
        version=1,
        action=action,
        status="active",
        condition=condition,
        trigger_id=None,
    )


@test()
def an_empty_readiness_renders_nothing() -> None:
    """No todos means no digest block, so the prompt carries no extra weight."""
    assert_eq(render_todo_digest(TodoReadiness(ready=[], waiting=[])), "")


@test()
def ready_todos_are_listed_for_proactive_surfacing() -> None:
    """Ready todos appear under a proactive-surfacing instruction."""
    digest = render_todo_digest(
        TodoReadiness(ready=[_todo("call the dentist")], waiting=[])
    )
    assert_in("call the dentist", digest)
    assert_in("Ready now", digest)


@test()
def waiting_todos_carry_their_condition_and_a_relevance_gate() -> None:
    """Waiting todos surface their condition under a relevance-gated instruction."""
    todo = _todo("bring the book", condition="next time I visit Ana")
    digest = render_todo_digest(TodoReadiness(ready=[], waiting=[todo]))
    assert_in("bring the book", digest)
    assert_in("next time I visit Ana", digest)
    assert_in("relevant", digest)


@test()
def a_waiting_deadline_is_rendered_when_present() -> None:
    """A waiting todo's deadline shows in its digest line."""
    todo = _todo("renew passport")
    deadline = datetime(2099, 1, 1, 9, 0, tzinfo=UTC)
    digest = render_todo_digest(
        TodoReadiness(ready=[], waiting=[todo], deadlines={todo.id: deadline})
    )
    assert_in("2099-01-01", digest)


@test()
def compose_appends_the_digest_after_the_persona() -> None:
    """The composed prompt keeps the persona prefix and appends the digest."""
    digest = render_todo_digest(
        TodoReadiness(ready=[_todo("call the dentist")], waiting=[])
    )
    composed = compose_conversation_prompt(digest)
    assert_true(composed.startswith(CONVERSATION_SYSTEM_PROMPT))
    assert_in("call the dentist", composed)


@test()
def compose_with_no_digest_is_the_bare_persona() -> None:
    """An empty digest yields exactly the constant persona."""
    assert_eq(compose_conversation_prompt(""), CONVERSATION_SYSTEM_PROMPT)
    assert_not_in("standing todos", compose_conversation_prompt(""))
