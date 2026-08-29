"""Pydantic validation returned as typed success or failure values."""

from functools import cache
from typing import cast

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeForm

from snekok.result import Err, Ok, Result

__all__ = ["validate_python"]


@cache
def _type_adapter(annotation: object) -> TypeAdapter[object]:
    """Reuse Pydantic's compiled validator for each annotation."""
    return TypeAdapter(cast("type[object]", annotation))


def validate_python[T](
    annotation: TypeForm[T], value: object
) -> Result[T, ValidationError]:
    """Validate a Python value without raising expected validation failures."""
    try:
        return Ok(cast("T", _type_adapter(annotation).validate_python(value)))
    except ValidationError as error:
        return Err(error)
