"""Source-independent Transcript content and Transcription state persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, Json
from snekok.types import NonBlankStr, NonEmptyStr, NonNegativeInt, PositiveInt
from snekok.validation import validate_python_unsafe
from snekql import sqlite
from snekql.sqlite import (
    PENDING_GENERATION,
    CurrentTimestamp,
    Database,
    DoUpdate,
    Fetched,
    ForeignKey,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
    delete,
    insert,
    select,
)

from tether.transcripts.contracts import TranscriptionKey, TranscriptSegment

type TranscriptionPersistedStatus = Literal[
    "retrying", "needs_review", "available", "unavailable"
]
type TranscriptionStatus = Literal[
    "pending", "retrying", "needs_review", "available", "unavailable"
]

_ZERO_FAILED_ATTEMPTS = validate_python_unsafe(NonNegativeInt, 0)


class _TranscriptionRow[S = Pending](Model[S, "_TranscriptionRow[Fetched]"]):
    """Internal row for acquisition state, separate from Transcript content."""

    __tablename__: ClassVar[str] = "transcription"

    id: sqlite.GenCol[PositiveInt] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    key: sqlite.Col[TranscriptionKey] = Text(nullable=False, unique=True)
    status: sqlite.Col[TranscriptionPersistedStatus] = Text(nullable=False)
    failed_attempts: sqlite.Col[NonNegativeInt] = Integer(default=_ZERO_FAILED_ATTEMPTS)
    last_error: sqlite.Col[NonEmptyStr | None] = Text(default=None, nullable=True)
    next_attempt_at: sqlite.Col[UtcDatetime | None] = Text(nullable=True, default=None)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class _TranscriptionSetting[S = Pending](Model[S, "_TranscriptionSetting[Fetched]"]):
    """Internal key/value storage for provider acquisition policy."""

    __tablename__: ClassVar[str] = "transcription_setting"

    key: sqlite.Col[str] = Text(primary_key=True)
    value: sqlite.Col[str] = Text(nullable=False)


class _TranscriptRow[S = Pending](Model[S, "_TranscriptRow[Fetched]"]):
    """Internal row containing only a completed Transcript."""

    __tablename__: ClassVar[str] = "transcript"

    id: sqlite.GenCol[PositiveInt] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    transcription_id: sqlite.FKCol[_TranscriptionRow, PositiveInt] = ForeignKey(
        _TranscriptionRow.id, nullable=False, unique=True, on_delete="CASCADE"
    )
    source: sqlite.Col[NonEmptyStr | None] = Text(nullable=True, default=None)
    text: sqlite.Col[NonBlankStr] = Text(nullable=False)
    segments: sqlite.Col[Json[tuple[TranscriptSegment, ...]]] = Text(nullable=False)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class Transcript(BaseModel):
    """Completed source-independent transcript content."""

    id: PositiveInt
    source: NonEmptyStr | None
    text: NonBlankStr
    segments: tuple[TranscriptSegment, ...]
    created_at: UtcDatetime
    updated_at: UtcDatetime

    model_config = ConfigDict(frozen=True, from_attributes=True)


class _TranscriptionStateBase(BaseModel):
    """Identity and timestamps shared by persisted Transcription states."""

    key: TranscriptionKey
    created_at: UtcDatetime
    updated_at: UtcDatetime

    model_config = ConfigDict(frozen=True, from_attributes=True, extra="ignore")


class TranscriptionRetrying(_TranscriptionStateBase):
    """A Transcription waiting until its next provider attempt."""

    status: Literal["retrying"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr
    next_attempt_at: UtcDatetime


class TranscriptionReviewNeeded(_TranscriptionStateBase):
    """A Transcription awaiting a human decision after provider exhaustion."""

    status: Literal["needs_review"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr


class TranscriptionAvailable(_TranscriptionStateBase):
    """A completed Transcription and the Transcript it produced."""

    status: Literal["available"]
    transcript: Transcript


class TranscriptionUnavailable(_TranscriptionStateBase):
    """A Transcription whose human settled that acquisition should stop."""

    status: Literal["unavailable"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr


type TranscriptionState = Annotated[
    TranscriptionRetrying
    | TranscriptionReviewNeeded
    | TranscriptionAvailable
    | TranscriptionUnavailable,
    Field(discriminator="status"),
]


class _TranscriptionFailureWrite(BaseModel):
    """Validated fields shared by failed acquisition transitions."""

    failed_attempts: NonNegativeInt = Field(gt=0)
    last_error: NonEmptyStr

    model_config = ConfigDict(frozen=True)


class _TranscriptionRetryingWrite(_TranscriptionFailureWrite):
    """Validated fields for a retrying Transcription."""

    next_attempt_at: UtcDatetime


class _TranscriptWrite(BaseModel):
    """Validated fields for a completed Transcript."""

    source: NonEmptyStr | None
    text: NonBlankStr
    segments: tuple[TranscriptSegment, ...]

    model_config = ConfigDict(frozen=True)


class TranscriptionStoreInvariantError(Exception):
    """Raised when persisted Transcription rows violate their state shape."""


_TRANSCRIPT_MIGRATIONS: dict[str, str] = {
    "016_create_transcription": (
        'CREATE TABLE "transcription" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
        '"key" TEXT NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"failed_attempts" INTEGER NOT NULL, '
        '"last_error" TEXT, '
        '"next_attempt_at" TEXT, '
        '"created_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    "016_create_index_ux_transcription_key": (
        'CREATE UNIQUE INDEX "ux_transcription_key" ON "transcription" ("key")'
    ),
    "016_create_transcription_setting": (
        'CREATE TABLE "transcription_setting" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    ),
    "016_create_transcript": (
        'CREATE TABLE "transcript" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
        '"transcription_id" INTEGER NOT NULL, '
        '"source" TEXT, '
        '"text" TEXT NOT NULL, '
        '"segments" TEXT NOT NULL, '
        '"created_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        'FOREIGN KEY ("transcription_id") REFERENCES "transcription" ("id") '
        "ON DELETE CASCADE"
        ") STRICT"
    ),
    "016_create_index_ux_transcript_transcription_id": (
        'CREATE UNIQUE INDEX "ux_transcript_transcription_id" '
        'ON "transcript" ("transcription_id")'
    ),
}


def transcript_migrations() -> dict[str, str]:
    """Return the ordered source-independent Transcript migration chain."""
    return dict(_TRANSCRIPT_MIGRATIONS)


async def create_transcript_schema(database: Database) -> None:
    """Create source-independent Transcript and Transcription storage.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_transcript_schema(database)
    """
    await database.migrate(transcript_migrations())


class TranscriptionSettings:
    """Persist source-independent settings used by transcript providers."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def read(self, key: str) -> str | None:
        """Read one setting."""
        async with self.database.transaction() as transaction:
            row = await transaction.fetch_one_or_none(
                select(_TranscriptionSetting).where(_TranscriptionSetting.key.eq(key))
            )
        return row.value if row is not None else None

    async def write(self, key: str, value: str) -> None:
        """Insert or replace one setting."""
        async with self.database.transaction(mode="immediate") as transaction:
            await transaction.execute(
                insert(_TranscriptionSetting(key=key, value=value)).on_conflict(
                    _TranscriptionSetting.key,
                    action=DoUpdate(_TranscriptionSetting.value.to_inserted()),
                )
            )

    async def keys(self, prefix: str) -> set[str]:
        """Return setting keys beginning with `prefix`."""
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(
                select(_TranscriptionSetting).where(
                    _TranscriptionSetting.key.like(f"{prefix}%")
                )
            )
        return {row.key for row in rows}


class TranscriptionStore:
    """Own Transcription state changes and completed Transcript content.

    ```python
    store = TranscriptionStore(database)
    await store.save_available(
        TranscriptionKey("recording:weekly-sync"),
        source="manual",
        text="The team agreed to ship Friday.",
        segments=(),
    )
    ```
    """

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def read(self, key: TranscriptionKey) -> TranscriptionState | None:
        """Read one Transcription, or `None` while it is pending."""
        async with self.database.transaction() as transaction:
            row = await transaction.fetch_one_or_none(
                select(_TranscriptionRow).where(_TranscriptionRow.key.eq(key))
            )
            if row is None:
                return None
            transcript = await self._read_transcript(transaction, row)
        return self._state(row, transcript)

    async def read_many(
        self, keys: Sequence[TranscriptionKey]
    ) -> dict[TranscriptionKey, TranscriptionState]:
        """Read materialized states for the requested Transcriptions."""
        if not keys:
            return {}
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(
                select(_TranscriptionRow).where(_TranscriptionRow.key.in_(*keys))
            )
            transcription_ids = [row.id for row in rows]
            transcript_rows = (
                await transaction.fetch_all(
                    select(_TranscriptRow).where(
                        _TranscriptRow.transcription_id.in_(*transcription_ids)
                    )
                )
                if transcription_ids
                else []
            )
        transcript_by_transcription = {
            row.transcription_id: validate_python_unsafe(Transcript, row)
            for row in transcript_rows
        }
        return {
            row.key: self._state(row, transcript_by_transcription.get(row.id))
            for row in rows
        }

    async def save_retrying(
        self,
        key: TranscriptionKey,
        *,
        failed_attempts: int,
        last_error: str,
        next_attempt_at: datetime,
    ) -> None:
        """Record a temporary failure and its next eligible attempt time."""
        fields = validate_python_unsafe(
            _TranscriptionRetryingWrite,
            {
                "failed_attempts": failed_attempts,
                "last_error": last_error,
                "next_attempt_at": next_attempt_at,
            },
        )
        async with self.database.transaction(mode="immediate") as transaction:
            row = await self._write_state(
                transaction,
                _TranscriptionRow(
                    key=key,
                    status="retrying",
                    failed_attempts=fields.failed_attempts,
                    last_error=fields.last_error,
                    next_attempt_at=fields.next_attempt_at,
                ),
            )
            await transaction.execute(
                delete(_TranscriptRow).where(_TranscriptRow.transcription_id.eq(row.id))
            )

    async def save_available(
        self,
        key: TranscriptionKey,
        *,
        source: str | None,
        text: str,
        segments: tuple[TranscriptSegment, ...],
    ) -> None:
        """Record a completed Transcript and clear acquisition failures."""
        fields = validate_python_unsafe(
            _TranscriptWrite,
            {"segments": segments, "source": source, "text": text},
        )
        async with self.database.transaction(mode="immediate") as transaction:
            transcription = await self._write_state(
                transaction,
                _TranscriptionRow(
                    key=key,
                    status="available",
                    failed_attempts=_ZERO_FAILED_ATTEMPTS,
                    last_error=None,
                    next_attempt_at=None,
                ),
            )
            await transaction.execute(
                insert(
                    _TranscriptRow(
                        transcription_id=transcription.id,
                        source=fields.source,
                        text=fields.text,
                        segments=fields.segments,
                    )
                ).on_conflict(
                    _TranscriptRow.transcription_id,
                    action=DoUpdate(
                        _TranscriptRow.source.to_inserted(),
                        _TranscriptRow.text.to_inserted(),
                        _TranscriptRow.segments.to_inserted(),
                        _TranscriptRow.updated_at.to(CurrentTimestamp),
                    ),
                )
            )

    async def save_review_needed(
        self,
        key: TranscriptionKey,
        *,
        failed_attempts: int,
        last_error: str,
    ) -> None:
        """Record provider exhaustion while waiting for a human decision."""
        fields = validate_python_unsafe(
            _TranscriptionFailureWrite,
            {"failed_attempts": failed_attempts, "last_error": last_error},
        )
        await self._save_failure(
            key,
            status="needs_review",
            failed_attempts=fields.failed_attempts,
            last_error=fields.last_error,
        )

    async def save_unavailable(
        self,
        key: TranscriptionKey,
        *,
        failed_attempts: int,
        last_error: str,
    ) -> None:
        """Record the human decision to stop acquisition."""
        fields = validate_python_unsafe(
            _TranscriptionFailureWrite,
            {"failed_attempts": failed_attempts, "last_error": last_error},
        )
        await self._save_failure(
            key,
            status="unavailable",
            failed_attempts=fields.failed_attempts,
            last_error=fields.last_error,
        )

    async def restart(self, key: TranscriptionKey) -> None:
        """Return a Transcription to pending by removing materialized state."""
        async with self.database.transaction(mode="immediate") as transaction:
            await transaction.execute(
                delete(_TranscriptionRow).where(_TranscriptionRow.key.eq(key))
            )

    async def _save_failure(
        self,
        key: TranscriptionKey,
        *,
        status: Literal["needs_review", "unavailable"],
        failed_attempts: NonNegativeInt,
        last_error: NonEmptyStr,
    ) -> None:
        """Replace any completed Transcript with one settled failure state."""
        async with self.database.transaction(mode="immediate") as transaction:
            row = await self._write_state(
                transaction,
                _TranscriptionRow(
                    key=key,
                    status=status,
                    failed_attempts=failed_attempts,
                    last_error=last_error,
                    next_attempt_at=None,
                ),
            )
            await transaction.execute(
                delete(_TranscriptRow).where(_TranscriptRow.transcription_id.eq(row.id))
            )

    @staticmethod
    async def _write_state(
        transaction: sqlite.Transaction,
        state: _TranscriptionRow[Pending],
    ) -> _TranscriptionRow[Fetched]:
        """Upsert state while retaining the Transcription's identity and birth time."""
        await transaction.execute(
            insert(state).on_conflict(
                _TranscriptionRow.key,
                action=DoUpdate(
                    _TranscriptionRow.status.to_inserted(),
                    _TranscriptionRow.failed_attempts.to_inserted(),
                    _TranscriptionRow.last_error.to_inserted(),
                    _TranscriptionRow.next_attempt_at.to_inserted(),
                    _TranscriptionRow.updated_at.to(CurrentTimestamp),
                ),
            )
        )
        return await transaction.fetch_one(
            select(_TranscriptionRow).where(_TranscriptionRow.key.eq(state.key))
        )

    @staticmethod
    async def _read_transcript(
        transaction: sqlite.Transaction,
        transcription: _TranscriptionRow[Fetched],
    ) -> Transcript | None:
        row = await transaction.fetch_one_or_none(
            select(_TranscriptRow).where(
                _TranscriptRow.transcription_id.eq(transcription.id)
            )
        )
        return validate_python_unsafe(Transcript, row) if row is not None else None

    @staticmethod
    def _state(
        row: _TranscriptionRow[Fetched], transcript: Transcript | None
    ) -> TranscriptionState:
        """Build one valid state and reject an available row without content."""
        values: dict[str, object] = {
            "created_at": row.created_at,
            "failed_attempts": row.failed_attempts,
            "key": row.key,
            "last_error": row.last_error,
            "next_attempt_at": row.next_attempt_at,
            "status": row.status,
            "transcript": transcript,
            "updated_at": row.updated_at,
        }
        if row.status == "available" and transcript is None:
            message = f"available Transcription {row.key!r} has no Transcript"
            raise TranscriptionStoreInvariantError(message)
        return validate_python_unsafe(TranscriptionState, values)
