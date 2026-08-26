"""Browser-safe domain state for server-owned provider authorization."""

from dataclasses import dataclass
from typing import Literal

ProviderAuthState = Literal["authorizing", "connected", "disconnected", "error"]


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """Validated data for completing a provider device authorization."""

    expires_in_seconds: int | None
    user_code: str
    verification_uri: str


@dataclass(frozen=True, slots=True)
class ProviderAuthStatus:
    """Current server credential or recovery-attempt state."""

    error: str | None = None
    expires_in_seconds: int | None = None
    state: ProviderAuthState = "disconnected"
    user_code: str | None = None
    verification_uri: str | None = None
