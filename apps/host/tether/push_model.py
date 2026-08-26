"""Domain values for Web Push subscription and delivery."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PushStatus:
    """Live subscription count and endpoint-specific membership."""

    subscribed: bool
    count: int


@dataclass(frozen=True, slots=True)
class VapidConfig:
    """Credentials used to authenticate browser push messages."""

    private_key: str
    public_key: str
    subject: str
