"""Typed Bucket Item payloads, validation, and identity derivation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

type ItemType = Literal["movie", "place", "book", "travel", "purchase"]
"""The kind of a Bucket item; determines which payload fields it carries."""

type PurchaseDecision = Literal["buy", "wait", "need-more-info"]
"""The human's current decision about a planned purchase."""

type DedupSeverity = Literal["none", "warn", "inform"]
"""How loudly dedup speaks about pre-existing duplicates of an Added item."""

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class EmptyIntentContextError(Exception):
    """Raised when intent context is blank after trimming whitespace."""


class InvalidItemDataError(Exception):
    """Raised when an item-type payload fails its type's validation."""


class MovieData(BaseModel):
    """The payload fields a `movie` Bucket item carries."""

    title: str
    year: int | None = None


class PlaceData(BaseModel):
    """The payload fields a `place` Bucket item carries."""

    name: str
    location: str | None = None


class BookData(BaseModel):
    """The payload fields a `book` Bucket item carries."""

    title: str
    author: str | None = None


class TravelData(BaseModel):
    """The payload fields a `travel` Bucket item carries."""

    destination: str
    season: str | None = None


class PurchaseData(BaseModel):
    """The context and current decision carried by a `purchase` Bucket item."""

    name: str
    price: str | None = None
    store: str | None = None
    decision_factors: list[str] = Field(default_factory=list)
    decision: PurchaseDecision | None = None


def _normalise_key(text: str) -> str:
    """Collapse a payload string to its dedup-comparison form.

    Dedup is about identity, not presentation, so case and surrounding/internal
    whitespace are noise: "The  Matrix" and "the matrix" are the same intention.
    """
    return " ".join(text.lower().split())


def _dedup_with_optional(base: str, optional: str | int | None) -> str:
    """Append one normalized distinguishing field to a dedup identity."""
    if optional is None:
        return base
    suffix = str(optional) if isinstance(optional, int) else _normalise_key(optional)
    return f"{base}|{suffix}"


def normalise_intent(intent_context: str | None) -> str:
    """Trim intent context, rejecting an explicitly-blank reason.

    Intent context answers "why did I save this?" months later. It is optional
    at Add — an omitted reason (`None`) stores as `""`, so the item is Added
    immediately rather than blocked — but a reason that *was* supplied must not
    be blank whitespace. It can be attached or replaced later through
    `BucketItemService.set_intent`.
    """
    if intent_context is None:
        return ""
    normalised = intent_context.strip()
    if not normalised:
        msg = "intent context must not be blank"
        raise EmptyIntentContextError(msg)
    return normalised


@dataclass(frozen=True, slots=True)
class BucketItemDescription:
    """The derived facts an Add needs from a validated item-type payload."""

    data: dict[str, JsonValue]
    dedup_key: str
    title: str


def describe_item(
    item_type: ItemType, data: Mapping[str, object]
) -> BucketItemDescription:
    """Validate a raw payload for its item type and derive its stored facts.

    Each item type owns how it builds its dedup key (the identity dedup compares)
    and its title (the human-facing, searchable projection of the payload). The
    raw payload is validated through the type's Pydantic model so a malformed
    payload is a well-formed domain error, never a corrupt row.
    """
    try:
        if item_type == "purchase":
            purchase = PurchaseData.model_validate(data)
            return BucketItemDescription(
                data=purchase.model_dump(mode="json"),
                dedup_key=_normalise_key(purchase.name),
                title=purchase.name,
            )
        match item_type:
            case "movie":
                movie = MovieData.model_validate(data)
                return BucketItemDescription(
                    data=movie.model_dump(mode="json"),
                    dedup_key=_dedup_with_optional(
                        _normalise_key(movie.title), movie.year
                    ),
                    title=movie.title,
                )
            case "place":
                place = PlaceData.model_validate(data)
                return BucketItemDescription(
                    data=place.model_dump(mode="json"),
                    dedup_key=_dedup_with_optional(
                        _normalise_key(place.name), place.location
                    ),
                    title=place.name,
                )
            case "book":
                book = BookData.model_validate(data)
                return BucketItemDescription(
                    data=book.model_dump(mode="json"),
                    dedup_key=_dedup_with_optional(
                        _normalise_key(book.title), book.author
                    ),
                    title=book.title,
                )
            case "travel":
                travel = TravelData.model_validate(data)
                return BucketItemDescription(
                    data=travel.model_dump(mode="json"),
                    dedup_key=_dedup_with_optional(
                        _normalise_key(travel.destination), travel.season
                    ),
                    title=travel.destination,
                )
    except ValidationError as error:
        message = (
            f"invalid {item_type} payload: {error.errors(include_url=False)[0]['msg']}"
        )
        raise InvalidItemDataError(message) from error
