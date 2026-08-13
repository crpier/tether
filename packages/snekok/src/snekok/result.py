"""Typed success and failure values."""

from dataclasses import dataclass
from typing import Generic, TypeVar

_Value_co = TypeVar("_Value_co", covariant=True)
_Error_co = TypeVar("_Error_co", covariant=True)


# PEP 695 has no explicit variance syntax, and type checkers infer public
# dataclass fields as invariant even when frozen. Legacy `Generic` syntax keeps
# each immutable channel covariant.
@dataclass(frozen=True, slots=True)
class Ok(Generic[_Value_co]):  # noqa: UP046
    """A successful result containing `value`.

    >>> Ok(42).value
    42
    """

    value: _Value_co


@dataclass(frozen=True, slots=True)
class Err(Generic[_Error_co]):  # noqa: UP046
    """A failed result containing `error`.

    >>> Err("invalid input").error
    'invalid input'
    """

    error: _Error_co


type Result[T, E] = Ok[T] | Err[E]
"""A successful `Ok[T]` or failed `Err[E]` value."""
