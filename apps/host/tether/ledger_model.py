"""Domain values for generic Ledgers, revisions, and entries."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    UUID7,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

_ENUM_VALUE_MAX_LENGTH = 80

LEDGER_ENTRY_BATCH_LIMIT = 25
"""Maximum entries accepted atomically by one tool call."""

LEDGER_FIELD_LIMIT = 32
"""Maximum fields in one immutable Ledger schema revision."""

type LedgerScalarValue = StrictBool | StrictInt | StrictStr

type LedgerFieldType = Literal[
    "boolean",
    "date",
    "datetime",
    "decimal",
    "enum",
    "integer",
    "text",
]
type LedgerLifecycleStatus = Literal["active", "completed", "abandoned"]
type LedgerProposalKind = Literal["create", "revise"]
type LedgerProposalStatus = Literal["pending", "approved"]

type LedgerFieldId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    ),
]


class LedgerFieldDefinition(BaseModel):
    """One stable scalar field in an immutable Ledger revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deprecated: bool = False
    description: str = Field(min_length=1, max_length=500)
    enum_values: list[str] | None = Field(default=None, min_length=1, max_length=64)
    field_id: LedgerFieldId
    label: str = Field(min_length=1, max_length=80)
    required: bool
    type: LedgerFieldType
    unit: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("description", "label")
    @classmethod
    def required_text_is_meaningful(cls, value: str) -> str:
        """Reject whitespace-only schema meaning and display metadata."""
        normalized = value.strip()
        if not normalized:
            message = "Ledger field text must not be blank"
            raise ValueError(message)
        return normalized

    @field_validator("unit")
    @classmethod
    def optional_unit_is_meaningful(cls, value: str | None) -> str | None:
        """Normalize a supplied fixed unit without accepting blank metadata."""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            message = "Ledger field unit must not be blank"
            raise ValueError(message)
        return normalized

    @field_validator("enum_values")
    @classmethod
    def enum_values_are_bounded(
        cls,
        values: list[str] | None,
    ) -> list[str] | None:
        """Normalize a bounded human-readable enum vocabulary."""
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(
            not value or len(value) > _ENUM_VALUE_MAX_LENGTH for value in normalized
        ):
            message = "Ledger enum values must contain 1 to 80 characters"
            raise ValueError(message)
        return normalized

    @model_validator(mode="after")
    def enum_shape_matches_type(self) -> Self:
        """Keep enum choices exclusive to enum fields and unambiguous."""
        if self.type == "enum":
            if self.enum_values is None:
                message = "enum fields require enum_values"
                raise ValueError(message)
            if len(set(self.enum_values)) != len(self.enum_values):
                message = "enum_values must be unique"
                raise ValueError(message)
        elif self.enum_values is not None:
            message = "enum_values are allowed only for enum fields"
            raise ValueError(message)
        return self


class LedgerEntryQuery(BaseModel):
    """Bounded filters for querying Ledger records outside Memory Search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    field_equals: dict[str, LedgerScalarValue] | None = None
    include_superseded: bool = False
    ledger_id: UUID7 | None = None
    limit: int = Field(default=50, ge=1, le=100)
    q: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def filters_are_coherent(self) -> Self:
        """Require an ordered range and scope dynamic fields to one schema."""
        if (
            self.after is not None
            and self.before is not None
            and self.after > self.before
        ):
            message = "Ledger query after must not exceed before"
            raise ValueError(message)
        if self.field_equals and self.ledger_id is None:
            message = "Ledger field filters require ledger_id"
            raise ValueError(message)
        return self


class LedgerEntryDraft(BaseModel):
    """One complete schema-versioned entry proposed for atomic append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_at: AwareDatetime | None = None
    supersedes_entry_id: UUID7 | None = None
    values: dict[LedgerFieldId, LedgerScalarValue] = Field(
        min_length=1,
        max_length=LEDGER_FIELD_LIMIT,
    )


class LedgerDefinition(BaseModel):
    """One proposed immutable Ledger definition revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: list[LedgerFieldDefinition] = Field(
        min_length=1,
        max_length=LEDGER_FIELD_LIMIT,
    )
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1_000)
    status: LedgerLifecycleStatus = "active"

    @field_validator("name", "purpose")
    @classmethod
    def definition_text_is_meaningful(cls, value: str) -> str:
        """Normalize user-visible definition text and reject empty meaning."""
        normalized = value.strip()
        if not normalized:
            message = "Ledger name and purpose must not be blank"
            raise ValueError(message)
        return normalized

    @model_validator(mode="after")
    def field_identities_are_unique(self) -> Self:
        """A revision assigns one interpretation to every field identity."""
        field_ids = [field.field_id for field in self.fields]
        if len(set(field_ids)) != len(field_ids):
            message = "Ledger field_id values must be unique"
            raise ValueError(message)
        return self


__all__ = [
    "LEDGER_ENTRY_BATCH_LIMIT",
    "LEDGER_FIELD_LIMIT",
    "LedgerDefinition",
    "LedgerEntryDraft",
    "LedgerEntryQuery",
    "LedgerFieldDefinition",
    "LedgerFieldId",
    "LedgerFieldType",
    "LedgerLifecycleStatus",
    "LedgerProposalKind",
    "LedgerProposalStatus",
    "LedgerScalarValue",
]
