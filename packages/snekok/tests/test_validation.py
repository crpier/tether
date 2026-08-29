"""Behavior tests for Pydantic validation returned as `Result` values."""

from typing import ClassVar

from pydantic import GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema
from snektest import assert_eq, assert_isinstance, fail, test

from snekok.result import Err, Ok
from snekok.validation import validate_python


class _SchemaBuildCounter(int):
    """A type that records each Pydantic schema build."""

    schema_build_count: ClassVar[int] = 0

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: object, _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Record adapter construction through Pydantic's public schema hook."""
        cls.schema_build_count += 1
        return core_schema.int_schema()


@test(mark="fast")
def valid_python_input_returns_ok() -> None:
    """A value accepted by Pydantic is returned in `Ok`."""
    assert_eq(validate_python(int, "42"), Ok(42))


@test(mark="fast")
def invalid_python_input_returns_validation_error() -> None:
    """A Pydantic validation failure is returned in `Err`."""
    validation_result = validate_python(int, "not-an-integer")

    match validation_result:
        case Err(error):
            _ = assert_isinstance(error, ValidationError)
        case Ok(validated_value):
            fail(f"unexpected validated value: {validated_value}")
        case unexpected:
            fail(f"unexpected result variant: {unexpected}")


@test(mark="fast")
def repeated_validation_reuses_type_adapter() -> None:
    """Validation compiles one adapter for repeated use of the same annotation."""
    _SchemaBuildCounter.schema_build_count = 0

    assert_eq(validate_python(_SchemaBuildCounter, 1), Ok(1))
    assert_eq(validate_python(_SchemaBuildCounter, 2), Ok(2))
    assert_eq(_SchemaBuildCounter.schema_build_count, 1)
