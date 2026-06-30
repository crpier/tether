"""Tests for the pure transcript chunker.

The chunker is the segment-free seam that turns a long, plain-text transcript
into bounded, overlapping windows the embedder can index one vector at a time.
It is deterministic and dependency-free, so these tests pin its boundaries,
overlap, and coverage without a model or LanceDB.
"""

from __future__ import annotations

from snektest import assert_eq, assert_true, test

from tether.transcript_chunks import chunk_transcript


@test()
def empty_or_blank_text_yields_no_chunks() -> None:
    assert_eq(chunk_transcript(""), [])
    assert_eq(chunk_transcript("   \n  \t "), [])


@test()
def text_within_budget_is_a_single_normalized_chunk() -> None:
    chunks = chunk_transcript("hello   there\nworld", max_chars=2000)
    assert_eq(chunks, ["hello there world"])


@test()
def long_text_splits_into_multiple_bounded_chunks() -> None:
    words = [f"w{index}" for index in range(400)]
    text = " ".join(words)
    chunks = chunk_transcript(text, max_chars=100, overlap_chars=20)
    assert_true(len(chunks) > 1)
    for chunk in chunks:
        assert_true(len(chunk) <= 100)


@test()
def consecutive_chunks_overlap_to_preserve_context() -> None:
    words = [f"w{index}" for index in range(400)]
    chunks = chunk_transcript(" ".join(words), max_chars=100, overlap_chars=30)
    first_tail = chunks[0].split()[-1]
    assert_true(first_tail in chunks[1].split())


@test()
def chunks_cover_every_word_in_order() -> None:
    words = [f"w{index}" for index in range(250)]
    chunks = chunk_transcript(" ".join(words), max_chars=80, overlap_chars=15)
    seen: list[str] = []
    for chunk in chunks:
        for word in chunk.split():
            if word not in seen:
                seen.append(word)
    assert_eq(seen, words)


@test()
def a_single_oversized_token_becomes_its_own_chunk() -> None:
    giant = "x" * 50
    chunks = chunk_transcript(f"{giant} tail", max_chars=20, overlap_chars=5)
    assert_eq(chunks[0], giant)


@test()
def chunking_is_deterministic() -> None:
    text = " ".join(f"w{index}" for index in range(300))
    assert_eq(
        chunk_transcript(text, max_chars=120, overlap_chars=25),
        chunk_transcript(text, max_chars=120, overlap_chars=25),
    )
