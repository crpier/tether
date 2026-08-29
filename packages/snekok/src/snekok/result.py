"""Typed success and failure values."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Never, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_Value_co = TypeVar("_Value_co", covariant=True, default=Never)
_Error_co = TypeVar("_Error_co", covariant=True, default=Never)


class Result[ValueT, ErrorT](ABC):
    """A nominal success-or-failure value with two covariant channels."""

    __slots__ = ()

    @abstractmethod
    def map[MappedT](
        self, transform: Callable[[ValueT], MappedT]
    ) -> Result[MappedT, ErrorT]:
        """Transform the success value."""

    @abstractmethod
    def map_error[MappedErrorT](
        self, transform: Callable[[ErrorT], MappedErrorT]
    ) -> Result[ValueT, MappedErrorT]:
        """Transform the error value."""

    @abstractmethod
    def and_then[MappedT, AddedErrorT](
        self,
        transform: Callable[[ValueT], Result[MappedT, AddedErrorT]],
    ) -> Result[MappedT, ErrorT | AddedErrorT]:
        """Continue with another fallible operation."""

    @abstractmethod
    async def and_then_async[MappedT, AddedErrorT](
        self,
        transform: Callable[[ValueT], Awaitable[Result[MappedT, AddedErrorT]]],
    ) -> Result[MappedT, ErrorT | AddedErrorT]:
        """Continue with an asynchronous fallible operation."""

    @abstractmethod
    def unwrap(self) -> ValueT:
        """Return the success value or raise for a failure."""

    @abstractmethod
    def unwrap_error(self) -> ErrorT:
        """Return the failure value or raise for a success."""


# PEP 695 has no explicit variance syntax, and type checkers infer public
# dataclass fields as invariant even when frozen. Legacy `Generic` syntax keeps
# each immutable channel covariant.
@dataclass(frozen=True, slots=True)
class Ok(
    Result[_Value_co, _Error_co],
    Generic[_Value_co, _Error_co],
):
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
        self, transform: Callable[[_Error_co], MappedErrorT]
    ) -> Ok[_Value_co, MappedErrorT]:
        """Preserve success while changing the phantom error channel."""
        del transform
        return Ok(self.value)

    def and_then[MappedT, AddedErrorT](
        self,
        transform: Callable[[_Value_co], Result[MappedT, AddedErrorT]],
    ) -> Result[MappedT, _Error_co | AddedErrorT]:
        """Continue with another fallible operation."""
        return transform(self.value)

    async def and_then_async[MappedT, AddedErrorT](
        self,
        transform: Callable[[_Value_co], Awaitable[Result[MappedT, AddedErrorT]]],
    ) -> Result[MappedT, _Error_co | AddedErrorT]:
        """Continue with an asynchronous fallible operation."""
        return await transform(self.value)

    def unwrap(self) -> _Value_co:
        """Unwrap the value from the `Ok` variant."""
        return self.value

    def unwrap_error(self) -> Never:
        """Reject unwrapping an error from the `Ok` variant."""
        msg = "Ok.unwrap_error() called on an Ok"
        raise RuntimeError(msg)


@dataclass(frozen=True, slots=True)
class Err(
    Result[_Value_co, _Error_co],
    Generic[_Value_co, _Error_co],
):
    """A failed result containing `error`.

    >>> Err("invalid input").error
    'invalid input'
    """

    error: _Error_co

    def map[MappedT](
        self, transform: Callable[[_Value_co], MappedT]
    ) -> Err[MappedT, _Error_co]:
        """Preserve the error while changing the phantom success channel."""
        del transform
        return Err(self.error)

    def map_error[MappedErrorT](
        self, transform: Callable[[_Error_co], MappedErrorT]
    ) -> Err[_Value_co, MappedErrorT]:
        """Transform the expected error."""
        return Err(transform(self.error))

    def and_then[MappedT, AddedErrorT](
        self,
        transform: Callable[[_Value_co], Result[MappedT, AddedErrorT]],
    ) -> Err[MappedT, _Error_co | AddedErrorT]:
        """Preserve the error while widening the result channels."""
        del transform
        return Err(self.error)

    async def and_then_async[MappedT, AddedErrorT](
        self,
        transform: Callable[[_Value_co], Awaitable[Result[MappedT, AddedErrorT]]],
    ) -> Err[MappedT, _Error_co | AddedErrorT]:
        """Preserve the error without running an asynchronous continuation."""
        del transform
        return Err(self.error)

    def unwrap(self) -> Never:
        msg = "Err.unwrap() called on an Err"
        raise RuntimeError(msg)

    def unwrap_error(self) -> _Error_co:
        """Unwrap the error from the `Err` variant."""
        return self.error
