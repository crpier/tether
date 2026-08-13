"""Static typing contracts for Result."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snekok import Result


def widen_result(
    outcome: Result[int, ValueError],
) -> Result[object, Exception]:
    """Widen both immutable Result channels without reconstructing the value."""
    return outcome
