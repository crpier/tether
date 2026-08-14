"""Validated nominal scalar types with direct constructors."""

from typing import ClassVar, Self

from pydantic import GetCoreSchemaHandler, SecretStr
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

__all__ = ["NonBlankStr", "NonEmptySecretStr", "NonEmptyStr", "NonNegativeInt"]


class NonBlankStr(str):
    """A nominal string containing at least one non-whitespace character.

    ```python
    text = NonBlankStr("hello")
    assert text == "hello"
    ```
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        """Construct a nominal string while enforcing its non-blank invariant."""
        if not value.strip():
            code = "value_error"
            raise PydanticCustomError(code, "Value must not be blank")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: type[Self], _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Teach Pydantic to validate strings and retain the nominal class."""
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class NonEmptySecretStr(SecretStr):
    """A secret strings which contains least one character.

    ```python
    secret = NonEmptySecretStr("secret-value")
    assert secret.get_secret_value() == "secret-value"
    ```
    """

    __slots__ = ()

    _inner_schema: ClassVar[CoreSchema] = core_schema.str_schema(min_length=1)

    def __init__(self, secret_value: str) -> None:
        """Construct a nominal secret while enforcing its invariant."""
        if not secret_value:
            msg = "value_error"
            raise PydanticCustomError(
                msg,
                "Value must not be empty {value}",
            )
        super().__init__(secret_value)


class NonEmptyStr(str):
    """A nominal string known to contain at least one character.

    ```python
    text = NonEmptyStr("hello")
    assert text == "hello"
    ```
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        """Construct a nominal string while enforcing its invariant."""
        if not value:
            msg = "value_error"
            raise PydanticCustomError(
                msg,
                "Value must not be empty {value}",
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: type[Self], _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Teach Pydantic to validate strings and retain the nominal class."""
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(min_length=1)
        )


class NonNegativeInt(int):
    """A nominal integer greater than or equal to zero.

    ```python
    count = NonNegativeInt(3)
    assert count == 3
    ```
    """

    __slots__ = ()

    def __new__(cls, value: int) -> Self:
        """Construct a nominal integer while enforcing its invariant."""
        if value < 0:
            msg = "value_error"
            raise PydanticCustomError(
                msg,
                "Value must be greater than or equal to zero {value}",
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: type[Self], _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Teach Pydantic to validate integers and retain the nominal class."""
        return core_schema.no_info_after_validator_function(
            cls, core_schema.int_schema(ge=0)
        )
