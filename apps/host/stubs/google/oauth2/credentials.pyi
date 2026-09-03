from collections.abc import Mapping, Sequence
from typing import Self

class Credentials:
    expired: bool
    refresh_token: str | None
    scopes: Sequence[str] | None
    valid: bool

    @classmethod
    def from_authorized_user_info(
        cls,
        info: Mapping[str, object],
        scopes: Sequence[str] | None = ...,
    ) -> Self: ...
    def refresh(self, request: object, /) -> None: ...
    def to_json(self) -> str: ...
