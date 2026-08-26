"""Authenticated identity for Tether's single-tenant app boundary."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated app identity carried by cookie and bearer sessions.

    ```python
    principal = Principal(sub="app")
    assert principal.sub == "app"
    ```
    """

    sub: str
