"""Domain values for Product observations."""

from typing import Literal

type ProductObservationStatus = Literal["open", "resolved"]
"""Lifecycle state of feedback captured while dogfooding Tether."""
