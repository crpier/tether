from collections.abc import Sequence

class Credentials:
    expired: bool
    refresh_token: str | None
    scopes: Sequence[str] | None
    valid: bool

    def refresh(self, request: object, /) -> None: ...
    def to_json(self) -> str: ...
