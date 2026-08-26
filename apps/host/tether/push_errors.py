"""Typed expected failures from browser Web Push providers."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebPushGoneFailure:
    """The provider no longer recognizes a browser subscription endpoint."""

    endpoint: str


type WebPushFailure = WebPushGoneFailure
