"""Typed expected failures for KOReader statistics-file ingestion."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class EbookStatsSourceFailure:
    """The configured statistics file could not be statted or snapshotted."""

    operation: Literal["snapshot", "stat"]
    path: str


@dataclass(frozen=True, slots=True)
class EbookStatsParseFailure:
    """A private snapshot was not a readable KOReader SQLite database."""

    path: str


type EbookStatsFailure = EbookStatsParseFailure | EbookStatsSourceFailure
