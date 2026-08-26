"""Split one saved video's searchable text into bounded overlapping windows.

Combined title, description, and transcript text can exceed the embedder's input
window. This pure deterministic seam emits character-budgeted, whitespace-clean
chunks with enough neighbouring context to preserve boundary phrases.

>>> chunk_youtube_text("a b c d e", max_chars=4, overlap_chars=1)
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


def chunk_youtube_text(
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
    current_chars = 0
    for word in words:
        addition = len(word) + (1 if current else 0)
        if current and current_chars + addition > max_chars:
            chunks.append(" ".join(current))
            current = _overlap_tail(current, overlap_chars)
            current_chars = _measured(current)
            addition = len(word) + (1 if current else 0)
        current.append(word)
        current_chars += addition
    if current:
        chunks.append(" ".join(current))
    return chunks


def _overlap_tail(words: list[str], overlap_chars: int) -> list[str]:
    """The trailing run of `words` that fits within `overlap_chars`."""
    reversed_tail: list[str] = []
    tail_chars = 0
    for word in reversed(words):
        addition = len(word) + (1 if reversed_tail else 0)
        if tail_chars + addition > overlap_chars:
            break
        reversed_tail.append(word)
        tail_chars += addition
    return list(reversed(reversed_tail))
