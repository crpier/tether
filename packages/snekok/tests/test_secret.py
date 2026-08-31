"""Behavior tests for snekok's validated nominal scalar types."""

from pydantic import BaseModel, SecretStr, ValidationError
from snektest import assert_eq, assert_isinstance, assert_raises, fail, test

from snekok.result import Err, Ok
from snekok.types import NonBlankStr, NonEmptySecretStr, NonEmptyStr, NonNegativeInt
from snekok.validation import validate_python


@test(mark="fast")
def non_empty_str_accepts_content() -> None:
    """The non-empty alias accepts a string containing content."""
    assert_eq(validate_python(NonEmptyStr, "hello"), Ok("hello"))


@test(mark="fast")
def non_empty_str_rejects_empty_string() -> None:
    """The non-empty alias rejects an empty string."""
    _ = assert_isinstance(validate_python(NonEmptyStr, ""), Err)


@test(mark="fast")
def non_blank_str_accepts_surrounding_whitespace() -> None:
    """The non-blank alias retains whitespace around content."""
    assert_eq(validate_python(NonBlankStr, "  hello  "), Ok("  hello  "))


@test(mark="fast")
def non_blank_str_rejects_whitespace_only_string() -> None:
    """The non-blank alias rejects strings empty after stripping whitespace."""
    _ = assert_isinstance(validate_python(NonBlankStr, "   "), Err)


@test(mark="fast")
def non_negative_int_accepts_zero() -> None:
    """The non-negative alias accepts its lower boundary."""
    assert_eq(validate_python(NonNegativeInt, 0), Ok(0))


@test(mark="fast")
def non_negative_int_rejects_negative_integer() -> None:
    """The non-negative alias rejects negative integers."""
    _ = assert_isinstance(validate_python(NonNegativeInt, -1), Err)


class IntSettings(BaseModel):
    """Settings model exercising the public non-negative integer alias."""

    count: NonNegativeInt


@test(mark="fast")
def pydantic_accepts_non_negative_integer() -> None:
    """Pydantic accepts valid integer input for the constrained alias."""
    settings = IntSettings.model_validate({"count": 3})

    assert_eq(settings.count, 3)


@test(mark="fast")
def pydantic_rejects_negative_non_negative_integer() -> None:
    """Pydantic preserves the non-negative constraint at model boundaries."""
    with assert_raises(ValidationError):
        _ = IntSettings.model_validate({"count": -1})


class NonBlankStringSettings(BaseModel):
    """Settings model exercising the public non-blank string alias."""

    label: NonBlankStr


@test(mark="fast")
def pydantic_rejects_whitespace_only_non_blank_string() -> None:
    """Pydantic preserves the non-blank constraint at model boundaries."""
    with assert_raises(ValidationError):
        _ = NonBlankStringSettings.model_validate({"label": "   "})


class StringSettings(BaseModel):
    """Settings model exercising the public non-empty string alias."""

    label: NonEmptyStr


@test(mark="fast")
def pydantic_accepts_non_empty_string() -> None:
    """Pydantic accepts valid string input for the constrained alias."""
    settings = StringSettings.model_validate({"label": "hello"})

    assert_eq(settings.label, "hello")


@test(mark="fast")
def pydantic_rejects_empty_non_empty_string() -> None:
    """Pydantic preserves the non-empty constraint at model boundaries."""
    with assert_raises(ValidationError):
        _ = StringSettings.model_validate({"label": ""})


@test(mark="fast")
def non_empty_secret_accepts_content() -> None:
    """The non-empty secret alias accepts a secret containing content."""
    match validate_python(NonEmptySecretStr, "secret-value"):
        case Ok(secret):
            assert_eq(secret.get_secret_value(), "secret-value")
            assert_eq(str(secret), "**********")
        case Err(error):
            fail(f"unexpected validation error: {error}")
        case unexpected:
            fail(f"unexpected result variant: {unexpected}")


@test(mark="fast")
def non_empty_secret_rejects_empty_secret() -> None:
    """The non-empty secret alias rejects an empty secret."""
    _ = assert_isinstance(validate_python(NonEmptySecretStr, ""), Err)


class SecretSettings(BaseModel):
    """Settings model exercising the public secret alias."""

    api_key: NonEmptySecretStr


@test(mark="fast")
def pydantic_accepts_non_empty_secret() -> None:
    """Pydantic accepts a non-empty value and retains secret masking."""
    settings = SecretSettings.model_validate({"api_key": "secret-value"})

    assert_eq(settings.api_key.get_secret_value(), "secret-value")
    assert_eq(str(settings.api_key), "**********")


@test(mark="fast")
def pydantic_rejects_empty_secret() -> None:
    """Pydantic rejects an empty secret before settings can use it."""
    with assert_raises(ValidationError):
        _ = SecretSettings.model_validate({"api_key": ""})


@test(mark="fast")
def pydantic_validates_existing_secret_str() -> None:
    """Pydantic applies the constraint to an existing `SecretStr`."""
    with assert_raises(ValidationError):
        _ = SecretSettings.model_validate({"api_key": SecretStr("")})
