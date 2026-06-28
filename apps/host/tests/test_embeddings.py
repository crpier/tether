"""Behavior tests for the Embedder seam.

The `Embedder` protocol is the only thing the rest of the host knows about
embeddings; `fastembed`/`onnxruntime` are imported nowhere outside the real
implementation. These tests drive the seam: the deterministic `FakeEmbedder`
used everywhere in the suite, the vector<->bytes serialization the SQLite BLOB
relies on, and (opt-in, network) the real `FastEmbedder`.

The real-model test downloads ~130MB from HuggingFace, so it is gated behind
`TETHER_EMBED_REAL=1` and is a no-op otherwise (snektest has no skip verb).
"""

import math
import os
from collections.abc import Sequence

from snektest import assert_eq, assert_gt, assert_true, test

from tether.embeddings import (
    FakeEmbedder,
    FastEmbedder,
    vector_from_bytes,
    vector_to_bytes,
)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@test(mark="fast")
async def fake_embedder_produces_the_configured_dimension() -> None:
    """A FakeEmbedder yields vectors of exactly its declared dimension."""
    embedder = FakeEmbedder(vector_dim=64)

    vector = await embedder.embed_query("aisle seats")

    assert_eq(len(vector), 64)
    assert_eq(embedder.vector_dim, 64)


@test(mark="fast")
async def fake_embedder_is_deterministic() -> None:
    """The same text always embeds to the same vector."""
    embedder = FakeEmbedder(vector_dim=64)

    first = await embedder.embed_query("I prefer aisle seats")
    second = await embedder.embed_query("I prefer aisle seats")

    assert_eq(first, second)


@test(mark="fast")
async def fake_embedder_reflects_token_overlap() -> None:
    """Shared vocabulary embeds closer than disjoint vocabulary.

    The fake is a normalized bag-of-words so it carries real lexical signal —
    enough for downstream Search ranking tests to mean something without a
    model download."""
    embedder = FakeEmbedder(vector_dim=128)
    query = await embedder.embed_query("aisle seat preference")
    related = await embedder.embed_query("I prefer an aisle seat")
    unrelated = await embedder.embed_query("the mitochondria powers the cell")

    assert_gt(_cosine(query, related), _cosine(query, unrelated))


@test(mark="fast")
async def embed_documents_returns_one_vector_per_text() -> None:
    """Batch document embedding preserves order and count."""
    embedder = FakeEmbedder(vector_dim=32)

    vectors = await embedder.embed_documents(["first text", "second text", "third"])

    assert_eq(len(vectors), 3)
    assert_true(all(len(v) == 32 for v in vectors))


@test(mark="fast")
async def vectors_round_trip_through_bytes() -> None:
    """A float32 vector survives the SQLite BLOB serialization round trip."""
    vector = [0.0, 1.0, -1.0, 0.5, 0.25, -0.125]

    restored = vector_from_bytes(vector_to_bytes(vector))

    assert_eq(len(restored), len(vector))
    assert_true(all(abs(a - b) < 1e-6 for a, b in zip(vector, restored, strict=True)))


@test(mark="slow")
async def real_fastembed_embeds_at_the_model_dimension() -> None:
    """Opt-in: the real FastEmbedder loads and embeds at 384 dims.

    Gated behind TETHER_EMBED_REAL=1 because it downloads ~130MB on first run.
    """
    if os.environ.get("TETHER_EMBED_REAL") != "1":
        return
    embedder = FastEmbedder()
    query = await embedder.embed_query("remind me about the dentist")
    docs = await embedder.embed_documents(["dentist appointment tuesday"])

    assert_eq(embedder.vector_dim, 384)
    assert_eq(len(query), 384)
    assert_eq(len(docs), 1)
    assert_eq(len(docs[0]), 384)
    assert_gt(_cosine(query, docs[0]), _cosine(query, [1.0] + [0.0] * 383))
