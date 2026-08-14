"""Behavior tests for snekok's validated nominal scalar types."""

from pydantic import BaseModel, SecretStr, ValidationError
from snektest import assert_eq, assert_isinstance, assert_raises, test

from snekok import NonEmptySecretStr
from snekok.types import NonBlankStr, NonEmptyStr, NonNegativeInt


@test(mark="fast")
def non_empty_str_constructor_returns_nominal_string() -> None:
    """Constructing a valid non-empty string retains its nominal class."""
    text = NonEmptyStr("hello")

    assert_eq(text, "hello")
    _ = assert_isinstance(text, NonEmptyStr)


@test(mark="fast")
def non_empty_str_constructor_rejects_empty_string() -> None:
    """Direct construction preserves the non-empty invariant."""
    with assert_raises(ValueError):
        _ = NonEmptyStr("")


@test(mark="fast")
def non_blank_str_constructor_returns_nominal_string() -> None:
    """Constructing text with content retains its nominal class and original text."""
    text = NonBlankStr("  hello  ")

    assert_eq(text, "  hello  ")
    _ = assert_isinstance(text, NonBlankStr)


@test(mark="fast")
def non_blank_str_constructor_rejects_whitespace_only_string() -> None:
    """Direct construction rejects strings empty after stripping whitespace."""
    with assert_raises(ValueError):
        _ = NonBlankStr("   ")


@test(mark="fast")
def non_negative_int_constructor_returns_nominal_integer() -> None:
    """Constructing a valid non-negative integer retains its nominal class."""
    count = NonNegativeInt(3)

    assert_eq(count, 3)
    _ = assert_isinstance(count, NonNegativeInt)


@test(mark="fast")
def non_negative_int_constructor_rejects_negative_integer() -> None:
    """Direct construction preserves the non-negative invariant."""
    with assert_raises(ValueError):
        _ = NonNegativeInt(-1)


class IntSettings(BaseModel):
    """Settings model exercising the public non-negative integer class."""

    count: NonNegativeInt


@test(mark="fast")
def pydantic_returns_nominal_non_negative_integer() -> None:
    """Pydantic converts valid integer input to the nominal class."""
    settings = IntSettings.model_validate({"count": 3})

    _ = assert_isinstance(settings.count, NonNegativeInt)


@test(mark="fast")
def pydantic_rejects_negative_non_negative_integer() -> None:
    """Pydantic preserves the class invariant at model boundaries."""
    with assert_raises(ValidationError):
        _ = IntSettings.model_validate({"count": -1})


class NonBlankStringSettings(BaseModel):
    """Settings model exercising the public non-blank string class."""

    label: NonBlankStr


@test(mark="fast")
def pydantic_rejects_whitespace_only_non_blank_string() -> None:
    """Pydantic preserves the non-blank invariant at model boundaries."""
    with assert_raises(ValidationError):
        _ = NonBlankStringSettings.model_validate({"label": "   "})


class StringSettings(BaseModel):
    """Settings model exercising the public non-empty string class."""

    label: NonEmptyStr


@test(mark="fast")
def pydantic_returns_nominal_non_empty_string() -> None:
    """Pydantic converts valid string input to the nominal class."""
    settings = StringSettings.model_validate({"label": "hello"})

    _ = assert_isinstance(settings.label, NonEmptyStr)


@test(mark="fast")
def pydantic_rejects_empty_non_empty_string() -> None:
    """Pydantic preserves the class invariant at model boundaries."""
    with assert_raises(ValidationError):
        _ = StringSettings.model_validate({"label": ""})


@test(mark="fast")
def non_empty_secret_constructor_returns_nominal_secret() -> None:
    """Constructing a valid secret retains its nominal class."""
    secret = NonEmptySecretStr("secret-value")

    assert_eq(secret.get_secret_value(), "secret-value")
    _ = assert_isinstance(secret, NonEmptySecretStr)


@test(mark="fast")
def non_empty_secret_constructor_rejects_empty_secret() -> None:
    """Direct construction preserves the non-empty secret invariant."""
    with assert_raises(ValueError):
        _ = NonEmptySecretStr("")


class SecretSettings(BaseModel):
    """Settings model exercising the public secret annotation."""

    api_key: NonEmptySecretStr


@test(mark="fast")
def non_empty_secret_is_accepted() -> None:
    """Pydantic accepts a non-empty value and retains secret masking."""
    settings = SecretSettings.model_validate({"api_key": "secret-value"})

    assert_eq(settings.api_key.get_secret_value(), "secret-value")
    assert_eq(str(settings.api_key), "**********")


@test(mark="fast")
def pydantic_returns_nominal_non_empty_secret() -> None:
    """Pydantic converts valid secret input to the nominal class."""
    settings = SecretSettings.model_validate({"api_key": "secret-value"})

    _ = assert_isinstance(settings.api_key, NonEmptySecretStr)


@test(mark="fast")
def empty_secret_is_rejected() -> None:
    """Pydantic rejects an empty secret before settings can use it."""
    with assert_raises(ValidationError):
        _ = SecretSettings.model_validate({"api_key": ""})


@test(mark="fast")
def existing_secret_str_is_validated() -> None:
    """Pydantic validates an existing `SecretStr` through the same constraint."""
    with assert_raises(ValidationError):
        _ = SecretSettings.model_validate({"api_key": SecretStr("")})
