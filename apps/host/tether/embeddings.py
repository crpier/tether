"""The Embedder seam: text -> vector, and the SQLite BLOB serialization.

`Embedder` is the only embedding abstraction the rest of the host depends on.
The real implementation (`FastEmbedder`) is the sole importer of `fastembed`
and runs CPU-bound inference off the event loop via `run_in_executor`
(onnxruntime releases the GIL during inference). `FakeEmbedder` is a
deterministic, dependency-free stand-in used throughout the test suite; it is a
normalized bag-of-words, so it carries enough lexical signal for Search ranking
tests to be meaningful without a model download.

Vectors cross the seam as plain `list[float]`; `vector_to_bytes` /
`vector_from_bytes` convert to and from the float32 bytes stored in SQLite (the
canonical vector), from which the LanceDB index is rebuilt.

>>> embedder: Embedder = FakeEmbedder(vector_dim=384)
>>> vector = await embedder.embed_query("aisle seats")
>>> blob = vector_to_bytes(vector)
>>> vector_from_bytes(blob) == vector
True
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

import numpy as np
from fastembed import TextEmbedding

type Vector = list[float]
"""A dense embedding as plain floats; the seam's currency."""

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
"""FastEmbed model id backing the default in-host embedder."""

DEFAULT_VECTOR_DIM = 384
"""Output dimension of `DEFAULT_EMBEDDING_MODEL`; fixes the LanceDB schema."""


def vector_to_bytes(vector: Sequence[float]) -> bytes:
    """Serialize a vector to the float32 bytes stored in the SQLite BLOB."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def vector_from_bytes(data: bytes) -> Vector:
    """Read a vector back from its stored float32 bytes."""
    return [float(value) for value in np.frombuffer(data, dtype=np.float32).tolist()]


class Embedder(Protocol):
    """Text-to-vector capability, with separate document and query paths.

    Document and query embedding are distinct because some models (e.g. BGE)
    apply different prefixes to passages vs. queries; callers must use the path
    that matches the side they are embedding."""

    @property
    def model_name(self) -> str:
        """Identifier of the model producing the vectors."""
        ...

    @property
    def vector_dim(self) -> int:
        """Dimension of every vector this embedder returns."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        """Embed a batch of passages, one vector per input, order preserved."""
        ...

    async def embed_query(self, text: str) -> Vector:
        """Embed a single search query."""
        ...


class FakeEmbedder:
    """Deterministic normalized bag-of-words embedder for tests.

    Tokens hash into a fixed-dimension vector that is L2-normalized, so shared
    vocabulary yields high cosine similarity. No network, no model, no GIL
    concerns — but enough lexical signal to exercise ranking."""

    def __init__(
        self, *, vector_dim: int = DEFAULT_VECTOR_DIM, model_name: str = "fake"
    ) -> None:
        self._vector_dim: int = vector_dim
        self._model_name: str = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    def _embed(self, text: str) -> Vector:
        vector = [0.0] * self._vector_dim
        for token in text.lower().split():
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest) % self._vector_dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._embed(text)


class FastEmbedder:
    """In-host FastEmbed/ONNX embedder; the sole importer of `fastembed`.

    The ONNX model is loaded lazily on the first embed call (it downloads on
    first ever use), not at construction, so building the embedder is cheap and
    booting the host never blocks on a model download — the model materializes
    only when something is actually embedded. `model_name` / `vector_dim` are
    fixed constants, so the index schema can be opened before the model loads.
    Inference runs on the default executor to keep the event loop free."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        vector_dim: int = DEFAULT_VECTOR_DIM,
    ) -> None:
        self._model_name: str = model_name
        self._vector_dim: int = vector_dim
        self._model: TextEmbedding | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    def _ensure_model(self) -> TextEmbedding:
        """Load the ONNX model on first use; cached for the embedder's lifetime."""
        if self._model is None:
            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def _embed_documents_sync(self, texts: list[str]) -> list[Vector]:
        return [
            [float(value) for value in array.tolist()]
            for array in self._ensure_model().embed(texts)
        ]

    def _embed_query_sync(self, text: str) -> Vector:
        array = next(iter(self._ensure_model().query_embed([text])))
        return [float(value) for value in array.tolist()]

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_documents_sync, list(texts))

    async def embed_query(self, text: str) -> Vector:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_query_sync, text)
