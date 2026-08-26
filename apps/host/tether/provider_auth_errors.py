"""Typed expected failures for provider authorization and recovery."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProviderAuthorizationActiveFailure:
    """A recovery attempt already owns the provider credential helper."""


@dataclass(frozen=True, slots=True)
class ProviderAuthProcessFailure:
    """The provider helper could not complete its validated protocol."""

    operation: Literal["login", "status"]
    reason: str


type ProviderAuthFailure = ProviderAuthProcessFailure
