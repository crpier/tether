"""Validated nominal scalar aliases for Pydantic boundaries."""

from typing import Annotated, NewType

from annotated_types import Ge, MinLen, Predicate
from pydantic import BaseModel, ConfigDict, SecretStr


def _is_not_blank(value: str) -> bool:
    """Return whether a string contains at least one non-whitespace character."""
    return bool(value.strip())


_NonBlankStr = NewType("_NonBlankStr", str)
_NonEmptySecretStr = NewType("_NonEmptySecretStr", SecretStr)
_NonEmptyStr = NewType("_NonEmptyStr", str)
_NonNegativeInt = NewType("_NonNegativeInt", int)
_PositiveInt = NewType("_PositiveInt", int)

NonBlankStr = Annotated[_NonBlankStr, Predicate(_is_not_blank)]
NonEmptySecretStr = Annotated[_NonEmptySecretStr, MinLen(1)]
NonEmptyStr = Annotated[_NonEmptyStr, MinLen(1)]
NonNegativeInt = Annotated[_NonNegativeInt, Ge(0)]
PositiveInt = Annotated[_PositiveInt, Ge(1)]

type NonEmptyTuple[T] = tuple[T, *tuple[T, ...]]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
