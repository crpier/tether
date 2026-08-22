"""Resolve stable `tether://` Evidence references to their source records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import UUID7, PositiveInt
from snekql.sqlite import Database, Fetched, select

from tether.conversation_model import MessageRole
from tether.conversation_store import Message
from tether.health_connect import (
    HealthConnectEvidence,
    HealthConnectEvidenceResolver,
)

_MESSAGE_URI = re.compile(r"tether://message/(?P<message_id>[0-9A-Fa-f-]+)")
_EXERCISE_URI = re.compile(
    r"tether://health-connect/exercise/(?P<record_uid>[^@/\s]+)@v(?P<version_id>[1-9][0-9]*)"
)
_SLEEP_URI = re.compile(
    r"tether://health-connect/sleep/(?P<record_uid>[^@/\s]+)@v(?P<version_id>[1-9][0-9]*)"
)


class InvalidEvidenceReferenceError(Exception):
    """A string is not one of Tether's supported Evidence references."""


class EvidenceNotFoundError(Exception):
    """A valid Evidence reference no longer has a readable source record."""


@dataclass(frozen=True, slots=True)
class MessageEvidence:
    """One exact settled conversation message named by an Evidence reference."""

    content: str
    conversation_id: UUID7
    message_id: UUID7
    occurred_at: datetime
    role: MessageRole
    seq: PositiveInt
    uri: str


class EvidenceResolver:
    """Resolve supported Evidence URIs without exposing source-store schemas."""

    def __init__(
        self,
        database: Database,
        health_connect: HealthConnectEvidenceResolver,
    ) -> None:
        self._database = database
        self._health_connect = health_connect

    async def resolve(self, uri: str) -> HealthConnectEvidence | MessageEvidence:
        """Return the exact source named by `uri`, or a typed lookup failure."""
        message_match = _MESSAGE_URI.fullmatch(uri)
        if message_match is not None:
            return await self._resolve_message(uri, message_match.group("message_id"))
        exercise_match = _EXERCISE_URI.fullmatch(uri)
        if exercise_match is not None:
            return await self._resolve_health_connect(
                uri,
                kind="exercise",
                record_uid=exercise_match.group("record_uid"),
                version_id=int(exercise_match.group("version_id")),
            )
        sleep_match = _SLEEP_URI.fullmatch(uri)
        if sleep_match is not None:
            return await self._resolve_health_connect(
                uri,
                kind="sleep",
                record_uid=sleep_match.group("record_uid"),
                version_id=int(sleep_match.group("version_id")),
            )
        raise InvalidEvidenceReferenceError(uri)

    async def _resolve_message(self, uri: str, raw_message_id: str) -> MessageEvidence:
        try:
            message_id = UUID(raw_message_id)
        except ValueError as error:
            raise InvalidEvidenceReferenceError(uri) from error
        async with self._database.transaction() as transaction:
            message: Message[Fetched] | None = await transaction.fetch_one_or_none(
                select(Message).where(Message.id.eq(message_id))
            )
        if message is None:
            raise EvidenceNotFoundError(uri)
        return MessageEvidence(
            content=message.content,
            conversation_id=message.conversation_id,
            message_id=message.id,
            occurred_at=message.created_at,
            role=message.role,
            seq=message.seq,
            uri=uri,
        )

    async def _resolve_health_connect(
        self,
        uri: str,
        *,
        kind: Literal["exercise", "sleep"],
        record_uid: str,
        version_id: int,
    ) -> HealthConnectEvidence:
        evidence = await self._health_connect.resolve(
            kind,
            record_uid=record_uid,
            version_id=version_id,
        )
        if evidence is None:
            raise EvidenceNotFoundError(uri)
        return evidence


__all__ = [
    "EvidenceNotFoundError",
    "EvidenceResolver",
    "InvalidEvidenceReferenceError",
    "MessageEvidence",
]
