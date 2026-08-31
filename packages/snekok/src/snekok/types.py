"""Validated nominal scalar aliases for Pydantic boundaries."""

from typing import Annotated, NewType

from annotated_types import Ge, MinLen, Predicate
from pydantic import BaseModel, ConfigDict, SecretStr


def _is_not_blank(value: str) -> bool:
    """Return whether a string contains at least one non-whitespace character."""
    return bool(value.strip())


NonBlankStr = Annotated[NewType("NonBlankStr", str), Predicate(_is_not_blank)]
NonEmptySecretStr = Annotated[NewType("NonEmptySecretStr", SecretStr), MinLen(1)]
NonEmptyStr = Annotated[NewType("NonEmptyStr", str), MinLen(1)]
NonNegativeInt = Annotated[NewType("NonNegativeInt", int), Ge(0)]
PositiveInt = Annotated[NewType("PositiveInt", int), Ge(1)]

type NonEmptyTuple[T] = tuple[T, *tuple[T, ...]]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
