"""A generic hybrid retriever over one embedded LanceDB table.

This module is the *sole* importer of `lancedb`/`pyarrow` in the host. It owns
a single table at `<index_dir>/` shaped as `id` + optional string payload
columns + `content` + a fixed-size `vector`, and exposes the small async
surface the domain projections (`SearchIndex` for Memories, `TranscriptIndex`
for transcript chunks) re-shape into their own document/candidate types:

- `upsert` / `remove` / `rebuild` keep the projection in step with SQLite
  (which remains the canonical store; the table is disposable and rebuildable);
- `search` runs LanceDB's native full-text search over `content` and an exact
  flat-scan cosine search over the caller-supplied query vector, fusing the two
  with Reciprocal Rank Fusion (`RRFReranker`);
- `optimize` runs LanceDB's background hygiene and self-heals the known lance
  compaction corruption — surfaced as either a `RuntimeError` or a
  `pyo3_async_runtimes.RustPanic` (lance-format/lance#7653) — by rewriting the
  table from its own readable rows.

Design choices baked in here:

- *Native FTS only, no vector ANN index.* The FTS index is created once on
  table creation; new/edited rows are found immediately via flat-scan of the
  unindexed tail. At Tether's single-user scale exact cosine over a flat scan
  beats a lossy IVF_PQ index, so no vector index is ever built.
- *Domain-agnostic boundary.* `TableDocument` in, `TableHit` out. The adapter
  knows nothing about Memories, videos, tethering, or soft-deletes — domain
  invariants are enforced upstream against SQLite, and domain shapes live in
  the thin projections wrapping this class.

>>> table = await HybridLanceTable.open(
...     index_dir=Path(".tether/index"), table_name="memories", vector_dim=384
... )
>>> await table.upsert([TableDocument(id=memory_id, content=text, vector=vec)])
>>> hits = await table.search(text="dentist", vector=query_vec, limit=10)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

import pyarrow as pa
from lancedb import connect_async
from lancedb.index import FTS
from lancedb.rerankers import RRFReranker

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from lancedb.db import AsyncConnection
    from lancedb.table import AsyncTable

    from tether.logging import Logger

# Marker lance stamps on the "file a bug report" class of internal errors — the
# recoverable-by-rewrite failures (e.g. a fragment whose compaction batch-decode
# overruns its values buffer). Matched case-insensitively on the message.
_LANCE_INTERNAL_ERROR_MARKER = "encountered internal error"

# The same corruption class can panic instead of raising (lance-format/lance#7653),
# which pyo3 surfaces as this type. `pyo3_async_runtimes` is synthesized by the
# Rust runtime and is not `import`-able from Python, so detection matches the
# name/module pair pyo3 stamps on the instance rather than `isinstance`.
_RUST_PANIC_MODULE = "pyo3_async_runtimes"
_RUST_PANIC_NAME = "RustPanic"

_ID_COLUMN = "id"
_CONTENT_COLUMN = "content"
_VECTOR_COLUMN = "vector"
_SCORE_COLUMN = "_relevance_score"
"""Column LanceDB attaches to reranked hybrid results (the RRF score)."""


def _is_lance_internal_error(error: Exception) -> bool:
    """Whether `error` is the lance-internal ("file a bug report") failure class.

    Scoped to that marker on purpose: unrelated runtime failures (a full disk, a
    permissions error) must propagate rather than trigger a table rewrite."""
    return _LANCE_INTERNAL_ERROR_MARKER in str(error).lower()


def _is_rust_panic(error: Exception) -> bool:
    """Whether `error` is a `pyo3_async_runtimes.RustPanic` from the lance runtime.

    Unlike `_is_lance_internal_error`, there is no message to inspect: a panic
    is masked down to "rust future panicked: unknown error" by the time it
    reaches Python. Any RustPanic surfacing from `optimize()` is treated as the
    same corruption class, since that is the only known cause."""
    return (
        type(error).__module__ == _RUST_PANIC_MODULE
        and type(error).__name__ == _RUST_PANIC_NAME
    )


def _schema(payload_columns: tuple[str, ...], vector_dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field(_ID_COLUMN, pa.string()),
            *(pa.field(column, pa.string()) for column in payload_columns),
            pa.field(_CONTENT_COLUMN, pa.string()),
            pa.field(_VECTOR_COLUMN, pa.list_(pa.float32(), vector_dim)),
        ]
    )


async def _create_table(
    connection: AsyncConnection,
    *,
    table_name: str,
    payload_columns: tuple[str, ...],
    vector_dim: int,
) -> AsyncTable:
    """Create the table with its one-time FTS index over `content`.

    `with_position` stores token offsets so phrase queries (quoted terms, which
    the model emits freely) resolve instead of raising "position is not found
    but required for phrase queries". Later rows are found via flat-scan of the
    unindexed tail, so the index is never rebuilt."""
    table = await connection.create_table(
        table_name, schema=_schema(payload_columns, vector_dim)
    )
    await table.create_index(
        _CONTENT_COLUMN,
        # lancedb ships no py.typed, so pyright can't see FTS's dataclass
        # fields and reads its init as no-arg; the kwarg is valid at runtime.
        config=FTS(with_position=True),  # pyright: ignore[reportCallIssue]
    )
    return table


class VectorDimMismatchError(Exception):
    """Raised when an existing table's vector width disagrees with the request.

    The adapter never silently reshapes a table; resolving a dimension change
    (a different embedding model) is the reconciler's job via drop-and-rebuild."""


@dataclass(frozen=True, slots=True)
class TableDocument:
    """A unit to (re)index: an id, its searchable text, its embedding, and any
    extra payload column values the owning projection declared."""

    id: UUID
    content: str
    vector: Sequence[float]
    payload: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class TableHit:
    """A hybrid-search hit: id, stored text, payload columns, and RRF score."""

    id: UUID
    content: str
    score: float
    payload: Mapping[str, str] = field(default_factory=dict[str, str])


class HybridLanceTable:
    """Async hybrid retriever over an embedded LanceDB table."""

    def __init__(
        self,
        *,
        connection: AsyncConnection,
        payload_columns: tuple[str, ...],
        table: AsyncTable,
        table_name: str,
        vector_dim: int,
    ) -> None:
        self._connection: AsyncConnection = connection
        self._payload_columns: tuple[str, ...] = payload_columns
        self._table: AsyncTable = table
        self._table_name: str = table_name
        self._vector_dim: int = vector_dim

    @property
    def vector_dim(self) -> int:
        """Width of the vectors this table stores; fixes its schema."""
        return self._vector_dim

    @classmethod
    async def open(
        cls,
        *,
        index_dir: Path,
        table_name: str,
        vector_dim: int,
        payload_columns: Sequence[str] = (),
    ) -> HybridLanceTable:
        """Open the table at `index_dir`, creating it if it is absent.

        Idempotent: an existing dataset is reused as-is. If its vector width
        disagrees with `vector_dim`, raises `VectorDimMismatchError` rather than
        corrupting the projection. `connect_async` creates `index_dir` (and any
        missing parents) on first connect."""
        connection = await connect_async(str(index_dir))
        # list_tables() returns a ListTablesResponse, not a list — read .tables.
        existing_tables = (await connection.list_tables()).tables
        if table_name in existing_tables:
            table = await connection.open_table(table_name)
            await cls._verify_dimension(table, vector_dim)
        else:
            table = await _create_table(
                connection,
                table_name=table_name,
                payload_columns=tuple(payload_columns),
                vector_dim=vector_dim,
            )
        return cls(
            connection=connection,
            payload_columns=tuple(payload_columns),
            table=table,
            table_name=table_name,
            vector_dim=vector_dim,
        )

    async def upsert(self, documents: Sequence[TableDocument]) -> None:
        """Insert or replace documents by id (payload + content + vector)."""
        rows = [
            {
                _ID_COLUMN: str(document.id),
                **{
                    column: document.payload[column] for column in self._payload_columns
                },
                _CONTENT_COLUMN: document.content,
                _VECTOR_COLUMN: list(document.vector),
            }
            for document in documents
        ]
        if not rows:
            return
        _ = await (
            self._table.merge_insert(_ID_COLUMN)
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(rows)
        )

    async def remove(self, ids: Sequence[UUID]) -> None:
        """Delete documents by id; ids absent from the table are ignored."""
        if not ids:
            return
        # UUIDs are hex+dashes, so direct interpolation is injection-safe.
        quoted = ", ".join(f"'{identifier}'" for identifier in ids)
        await self._table.delete(f"{_ID_COLUMN} IN ({quoted})")

    async def search(
        self, *, text: str, vector: Sequence[float], limit: int
    ) -> list[TableHit]:
        """Hybrid search: native FTS on `text` + cosine on `vector`, fused by RRF."""
        rows = await (
            self._table.query()
            .nearest_to(list(vector))
            .nearest_to_text(text)
            .rerank(RRFReranker())
            .limit(limit)
            .to_list()
        )
        return [
            TableHit(
                id=UUID(str(row[_ID_COLUMN])),
                content=str(row[_CONTENT_COLUMN]),
                score=float(row[_SCORE_COLUMN]),
                payload={column: str(row[column]) for column in self._payload_columns},
            )
            for row in rows
        ]

    async def rebuild(self, documents: Sequence[TableDocument]) -> None:
        """Drop the table and reindex `documents` from scratch.

        Used when the embedding model changes: vectors from different models are
        incomparable, so the whole projection is rebuilt from SQLite."""
        await self._connection.drop_table(self._table_name)
        self._table = await _create_table(
            self._connection,
            table_name=self._table_name,
            payload_columns=self._payload_columns,
            vector_dim=self._vector_dim,
        )
        await self.upsert(documents)

    async def count(self) -> int:
        """Number of documents currently indexed."""
        return await self._table.count_rows()

    async def list_ids(self) -> set[UUID]:
        """Every document id currently in the table.

        Reconcilers diff this against SQLite's canonical set to drop orphans
        left behind by a missed event — the correctness backstop for the
        latency path."""
        rows = await self._table.query().select([_ID_COLUMN]).to_list()
        return {UUID(str(row[_ID_COLUMN])) for row in rows}

    async def optimize(self, *, logger: Logger) -> None:
        """Run LanceDB's background hygiene (compaction, index maintenance).

        Self-heals a class of upstream lance bug: a corrupt fragment whose
        compaction batch-decode overruns its values buffer ("Encountered internal
        error … Error decoding batch"). Every reconcile tick calls this, so an
        unhandled failure wedges the loop forever. The rows still read (only the
        wide compaction decode trips the bug), and this projection is disposable,
        so we salvage in place: read every row back out, recreate the table, and
        re-add them — no data lost and no re-embedding, since the vectors survive
        the round-trip. Any other failure propagates untouched.

        The same bug class can also panic instead of raising (lance-format/lance
        #7653, seen with `FTS(with_position=True)` merging an unindexed tail on
        lancedb 0.33.0): pyo3 surfaces that as `pyo3_async_runtimes.RustPanic`,
        an `Exception` subclass but not a `RuntimeError`. Any RustPanic here is
        salvaged the same way."""
        try:
            _ = await self._table.optimize()
        except RuntimeError as error:
            if not _is_lance_internal_error(error):
                raise
            logger.warning(
                "Lance table optimize hit an internal error; salvaging",
                table=self._table_name,
                error=str(error),
            )
            await self._salvage_rewrite()
            logger.info(
                "Lance table salvaged after internal error", table=self._table_name
            )
        except Exception as error:
            if not _is_rust_panic(error):
                raise
            logger.warning(
                "Lance table optimize hit a rust panic; salvaging",
                table=self._table_name,
                error=str(error),
            )
            await self._salvage_rewrite()
            logger.info("Lance table salvaged after rust panic", table=self._table_name)

    @staticmethod
    async def _verify_dimension(table: AsyncTable, vector_dim: int) -> None:
        schema = await table.schema()
        field_type = schema.field(_VECTOR_COLUMN).type
        if not pa.types.is_fixed_size_list(field_type):  # pragma: no cover - defensive
            message = f"index column {_VECTOR_COLUMN!r} is not a fixed-size vector"
            raise VectorDimMismatchError(message)
        existing = field_type.list_size
        if existing != vector_dim:
            message = (
                f"index vector width {existing} does not match requested {vector_dim}"
            )
            raise VectorDimMismatchError(message)

    async def _salvage_rewrite(self) -> None:
        """Rebuild the table from its own readable rows, then compact once.

        Reads survive the fragment corruption that compaction cannot, so the
        rows (id, payload columns, content, vector) round-trip losslessly into a
        fresh table. The final optimize runs on the wrapped table directly, not
        through the self-healing entrypoint, so a still-broken rewrite raises
        instead of looping."""
        rows = await self._table.query().to_list()
        await self._connection.drop_table(self._table_name)
        self._table = await _create_table(
            self._connection,
            table_name=self._table_name,
            payload_columns=self._payload_columns,
            vector_dim=self._vector_dim,
        )
        if rows:
            _ = await (
                self._table.merge_insert(_ID_COLUMN)
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(rows)
            )
        _ = await self._table.optimize()
