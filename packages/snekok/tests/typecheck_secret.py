"""Static typing contracts for nominal validated scalar aliases."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_type

from snekok.types import NonBlankStr, NonEmptySecretStr, NonEmptyStr, NonNegativeInt
from snekok.validation import validate_python

if TYPE_CHECKING:
    from pydantic import SecretStr


def validation_returns_nominal_types() -> None:
    """Prove validated values retain each alias's nominal static type."""
    assert_type(validate_python(NonBlankStr, "hello").unwrap(), NonBlankStr)
    assert_type(
        validate_python(NonEmptySecretStr, "secret").unwrap(),
        NonEmptySecretStr,
    )
    assert_type(validate_python(NonEmptyStr, "hello").unwrap(), NonEmptyStr)
    assert_type(validate_python(NonNegativeInt, 0).unwrap(), NonNegativeInt)


def require_non_empty_secret(secret: NonEmptySecretStr) -> None:
    """Accept only the nominal validated secret type."""
    assert_type(secret, NonEmptySecretStr)


def plain_secret_is_not_nominal(secret: SecretStr) -> None:
    """Prove an ordinary `SecretStr` cannot cross the nominal boundary."""
    require_non_empty_secret(secret)  # pyright: ignore[reportArgumentType]
