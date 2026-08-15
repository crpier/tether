"""Static typing contracts for nominal non-empty secrets."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_type

from snekok import NonEmptySecretStr
from snekok.types import NonBlankStr, NonEmptyStr, NonNegativeInt

if TYPE_CHECKING:
    from pydantic import SecretStr


def constructors_return_nominal_types() -> None:
    """Prove each direct constructor has its nominal return type."""
    assert_type(NonBlankStr("hello"), NonBlankStr)
    assert_type(NonEmptySecretStr("secret"), NonEmptySecretStr)
    assert_type(NonEmptyStr("hello"), NonEmptyStr)
    assert_type(NonNegativeInt(0), NonNegativeInt)


def require_non_empty_secret(secret: NonEmptySecretStr) -> None:
    """Accept only the nominal validated secret type."""
    assert_type(secret, NonEmptySecretStr)


def plain_secret_is_not_nominal(secret: SecretStr) -> None:
    """Prove an ordinary `SecretStr` cannot cross the nominal boundary."""
    require_non_empty_secret(secret)  # pyright: ignore[reportArgumentType]
