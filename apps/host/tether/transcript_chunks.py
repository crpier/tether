"""The transcript chunker: one long plain-text transcript -> bounded windows.

Transcripts run to thousands of tokens — far past the embedder's ~512-token
window — so a single vector per video would silently truncate to the opening
lines. This pure, deterministic seam splits a transcript into overlapping,
char-budgeted windows so each indexes as its own vector with enough neighbouring
context to stay coherent. Stored transcripts carry no segment timestamps, so it
works on whitespace-delimited words alone and emits plain text.

>>> chunk_transcript("a b c d e", max_chars=4, overlap_chars=1)
['a b', 'b c', 'c d', 'd e']
"""

from __future__ import annotations

# Default window (~512 tokens at ~4 chars/token) and the overlap carried into the
# next window so a phrase split across a boundary still lands whole in one chunk.
_DEFAULT_MAX_CHARS = 2000
_DEFAULT_OVERLAP_CHARS = 200


def _measured(words: list[str]) -> int:
    """Char length of `words` joined by single spaces."""
    if not words:
        return 0
    return sum(len(word) for word in words) + (len(words) - 1)


def chunk_transcript(
    text: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap_chars: int = _DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Split `text` into overlapping windows of at most `max_chars` characters.

    Whitespace is normalized to single spaces and never split mid-word, so each
    window is a clean run of words. When a window fills, the next one is seeded
    with a trailing run of up to `overlap_chars` characters of the previous
    window's words, preserving context across the boundary. A lone token longer
    than `max_chars` becomes its own (oversized) chunk rather than being dropped.
    """
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        addition = len(word) + (1 if current else 0)
        if current and _measured(current) + addition > max_chars:
            chunks.append(" ".join(current))
            current = _overlap_tail(current, overlap_chars)
        current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _overlap_tail(words: list[str], overlap_chars: int) -> list[str]:
    """The trailing run of `words` that fits within `overlap_chars`."""
    tail: list[str] = []
    for word in reversed(words):
        if _measured([word, *tail]) > overlap_chars:
            break
        tail.insert(0, word)
    return tail
