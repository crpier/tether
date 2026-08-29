"""Small, typed tools for treating expected failures as values."""

from snekok.result import Err, Ok, Result
from snekok.types import NonEmptySecretStr
from snekok.validation import validate_python

__all__ = ["Err", "NonEmptySecretStr", "Ok", "Result", "validate_python"]
