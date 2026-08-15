"""Typed success and failure values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Never, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_Value_co = TypeVar("_Value_co", covariant=True, default=Never)
_Error_co = TypeVar("_Error_co", covariant=True, default=Never)


# PEP 695 has no explicit variance syntax, and type checkers infer public
# dataclass fields as invariant even when frozen. Legacy `Generic` syntax keeps
# each immutable channel covariant.
@dataclass(frozen=True, slots=True)
class Ok(Generic[_Value_co, _Error_co]):
    """A successful result containing `value`.

    >>> Ok(42).value
    42
    """

    value: _Value_co

    def map[MappedT](
        self, transform: Callable[[_Value_co], MappedT]
    ) -> Ok[MappedT, _Error_co]:
        """Transform the success value."""
        return Ok(transform(self.value))

    def map_error[MappedErrorT](
        self, _transform: Callable[[_Error_co], MappedErrorT]
    ) -> Ok[_Value_co, MappedErrorT]:
        """Preserve success while changing the phantom error channel."""
        return Ok(self.value)

    def and_then[MappedT, AddedErrorT](
        self,
        transform: Callable[[_Value_co], Result[MappedT, AddedErrorT]],
    ) -> Result[MappedT, _Error_co | AddedErrorT]:
        """Continue with another fallible operation."""
        return transform(self.value)

    def unwrap(self) -> _Value_co:
        """Unwrap the value from the `Ok` variant."""
        return self.value


@dataclass(frozen=True, slots=True)
class Err(Generic[_Value_co, _Error_co]):
    """A failed result containing `error`.

    >>> Err("invalid input").error
    'invalid input'
    """

    error: _Error_co

    def map[MappedT](
        self, _transform: Callable[[_Value_co], MappedT]
    ) -> Err[MappedT, _Error_co]:
        """Preserve the error while changing the phantom success channel."""
        return Err(self.error)

    def map_error[MappedErrorT](
        self, transform: Callable[[_Error_co], MappedErrorT]
    ) -> Err[_Value_co, MappedErrorT]:
        """Transform the expected error."""
        return Err(transform(self.error))

    def and_then[MappedT, AddedErrorT](
        self,
        _transform: Callable[[_Value_co], Result[MappedT, AddedErrorT]],
    ) -> Err[MappedT, _Error_co | AddedErrorT]:
        """Preserve the error while widening the result channels."""
        return Err(self.error)

    def unwrap(self) -> Never:
        msg = "Err.unwrap() called on an Err"
        raise RuntimeError(msg)


type Result[T, E] = Ok[T, E] | Err[T, E]
"""A successful `Ok[T, E]` or failed `Err[T, E]` value."""
