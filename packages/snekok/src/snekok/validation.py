"""Pydantic validation returned as typed success or failure values."""

from functools import cache
from typing import cast

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeForm

from snekok.result import Err, Ok, Result


@cache
def _type_adapter[T](annotation: TypeForm[T]) -> TypeAdapter[T]:
    """Reuse Pydantic's compiled validator for each annotation."""
    return TypeAdapter(annotation)


def validate_python[T](
    annotation: TypeForm[T], value: object
) -> Result[T, ValidationError]:
    """Validate a Python value without raising expected validation failures."""
    try:
        return Ok(cast("T", _type_adapter(annotation).validate_python(value)))
    except ValidationError as error:
        return Err(error)


def validate_python_unsafe[T](annotation: TypeForm[T], value: object) -> T:
    """Validate a trusted Python boundary, raising `ValidationError` on defects."""
    return cast("T", _type_adapter(annotation).validate_python(value))


def validate_json[T](
    annotation: TypeForm[T], value: str | bytes | bytearray
) -> Result[T, ValidationError]:
    """Validate JSON input without raising expected validation failures."""
    try:
        return Ok(cast("T", _type_adapter(annotation).validate_json(value)))
    except ValidationError as error:
        return Err(error)
