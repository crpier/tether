"""Authenticated browser reads for stable Evidence references."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from fastapi import APIRouter
from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.conversation_model import MessageRole
from tether.email_evidence import EmailEvidence
from tether.evidence import (
    EvidenceNotFoundError,
    EvidenceResolver,
    InvalidEvidenceReferenceError,
    MessageEvidence,
)
from tether.health_connect import (
    HealthConnectExerciseEvidence,
    HealthConnectSleepEvidence,
)


class MessageEvidenceRead(BaseModel):
    """Browser-safe representation of one cited conversation message."""

    kind: Literal["message"] = "message"
    uri: str
    conversation_id: UUID7
    message_id: UUID7
    seq: PositiveInt
    role: MessageRole
    content: str
    occurred_at: datetime

    @classmethod
    def from_evidence(cls, evidence: MessageEvidence) -> MessageEvidenceRead:
        return cls(
            content=evidence.content,
            conversation_id=evidence.conversation_id,
            message_id=evidence.message_id,
            occurred_at=evidence.occurred_at,
            role=evidence.role,
            seq=evidence.seq,
            uri=evidence.uri,
        )


class EmailEvidenceRead(BaseModel):
    """Browser-safe representation of one promoted email source."""

    kind: Literal["email"] = "email"
    body_chars: int
    body_text: str
    body_truncated: bool
    captured_at: datetime
    content_hash: str
    date_header: str
    from_header: str
    gmail_message_id: str
    subject: str
    thread_id: str
    uri: str

    @classmethod
    def from_evidence(cls, evidence: EmailEvidence) -> EmailEvidenceRead:
        return cls.model_validate(evidence, from_attributes=True)


class ExerciseEvidenceRead(BaseModel):
    """Browser-safe representation of one historical exercise episode."""

    kind: Literal["health_connect_exercise"] = "health_connect_exercise"
    uri: str
    record_uid: str
    version_id: PositiveInt
    title: str | None
    start_time: datetime
    end_time: datetime
    duration_minutes: float
    exercise_type: str | None
    segment_count: int
    lap_count: int
    total_lap_meters: float | None

    @classmethod
    def from_evidence(
        cls, evidence: HealthConnectExerciseEvidence, *, uri: str
    ) -> ExerciseEvidenceRead:
        return cls(
            duration_minutes=evidence.duration_minutes,
            end_time=evidence.end_time,
            exercise_type=evidence.exercise_type,
            lap_count=evidence.lap_count,
            record_uid=evidence.record_uid,
            segment_count=evidence.segment_count,
            start_time=evidence.start_time,
            title=evidence.title,
            total_lap_meters=evidence.total_lap_meters,
            uri=uri,
            version_id=evidence.version_id,
        )


class SleepEvidenceRead(BaseModel):
    """Browser-safe representation of one historical sleep episode."""

    kind: Literal["health_connect_sleep"] = "health_connect_sleep"
    uri: str
    record_uid: str
    version_id: PositiveInt
    title: str | None
    start_time: datetime
    end_time: datetime
    duration_minutes: float
    stage_minutes: dict[str, float]

    @classmethod
    def from_evidence(
        cls, evidence: HealthConnectSleepEvidence, *, uri: str
    ) -> SleepEvidenceRead:
        return cls(
            duration_minutes=evidence.duration_minutes,
            end_time=evidence.end_time,
            record_uid=evidence.record_uid,
            stage_minutes=evidence.stage_minutes,
            start_time=evidence.start_time,
            title=evidence.title,
            uri=uri,
            version_id=evidence.version_id,
        )


class _EvidenceRuntime(Protocol):
    evidence_resolver: EvidenceResolver


def _resolver(request: Request) -> EvidenceResolver:
    runtime: _EvidenceRuntime = request.app.state.runtime
    return runtime.evidence_resolver


router = APIRouter()


@router.get(
    "/api/evidence",
    response_model=(
        EmailEvidenceRead
        | ExerciseEvidenceRead
        | MessageEvidenceRead
        | SleepEvidenceRead
    ),
)
async def resolve_evidence(
    request: Request, uri: str
) -> (
    EmailEvidenceRead
    | ExerciseEvidenceRead
    | MessageEvidenceRead
    | SleepEvidenceRead
    | Response
):
    """Resolve one stable Evidence reference for in-app inspection."""
    try:
        evidence = await _resolver(request).resolve(uri)
    except InvalidEvidenceReferenceError:
        return JSONResponse(
            {"detail": "unsupported Evidence reference"}, status_code=422
        )
    except EvidenceNotFoundError:
        return JSONResponse({"detail": "Evidence is unavailable"}, status_code=404)
    if isinstance(evidence, EmailEvidence):
        return EmailEvidenceRead.from_evidence(evidence)
    if isinstance(evidence, MessageEvidence):
        return MessageEvidenceRead.from_evidence(evidence)
    if isinstance(evidence, HealthConnectExerciseEvidence):
        return ExerciseEvidenceRead.from_evidence(evidence, uri=uri)
    return SleepEvidenceRead.from_evidence(evidence, uri=uri)


__all__ = [
    "EmailEvidenceRead",
    "ExerciseEvidenceRead",
    "MessageEvidenceRead",
    "SleepEvidenceRead",
    "router",
]
