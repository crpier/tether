"""Static typing contracts for Result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, assert_type

from snekok import Err, Ok

if TYPE_CHECKING:
    from snekok import Result


def variants_default_the_absent_channel_to_never() -> None:
    """Concrete constructors infer `Never` for their phantom channel."""
    assert_type(Ok(1), Ok[int, Never])
    assert_type(Err("invalid"), Err[Never, str])


def widen_result(
    outcome: Result[int, ValueError],
) -> Result[object, Exception]:
    """Widen both immutable Result channels without reconstructing the value."""
    return outcome


def map_result(outcome: Result[int, str]) -> Result[str, str]:
    """`map` changes only the success channel of a union-typed result."""
    return outcome.map(str)


def map_result_error(outcome: Result[int, str]) -> Result[int, int]:
    """`map_error` changes only the error channel of a union-typed result."""
    return outcome.map_error(len)


def stringify_positive(number: int) -> Result[str, ValueError]:
    """Return a fallible transformation used to prove channel composition."""
    if number < 0:
        return Err(ValueError("negative"))
    return Ok(str(number))


def chain_result(outcome: Result[int, str]) -> Result[str, str | ValueError]:
    """`and_then` preserves old errors and adds continuation errors."""
    return outcome.and_then(stringify_positive)
