/**
 * Incremental sentence extraction for streamed spoken replies (#545).
 *
 * The browser speaks complete sentences as they arrive instead of waiting for
 * the settled final text, cutting perceived latency dramatically on long
 * replies. Pure and framework-free: the caller owns what "emit" means
 * (normally normalize + enqueue on the speech player).
 *
 * MVP trade-off: sentence detection splits after any terminator followed by
 * whitespace, so abbreviations like "e.g. " split too. For speech cadence
 * that is acceptable; a lookahead heuristic can refine it later.
 */

export interface SpokenStream {
  /** Feeds one streamed delta; emits each newly completed sentence. */
  push(delta: string): void;
  /** Tool activity began: invalidate the emitted prefix. */
  restart(): void;
  /** Clears all state (new turn). */
  reset(): void;
  /** The portion of `finalText` that has not been emitted yet. */
  tail(finalText: string): string;
}

// A sentence terminator plus its trailing whitespace — the earliest point
// speech can safely begin without clipping words mid-phrase. Consuming the
// whitespace keeps emitted prefixes byte-aligned with the settled text.
const SENTENCE_BOUNDARY = /[.!?…]+["'”’)\]]*\s|[.!?…]+["'”’)\]]*$/;

export function createSpokenStream(
  emit: (sentence: string) => void,
  onRestart?: () => void,
): SpokenStream {
  let buffer = "";
  let emittedUpTo = 0;
  let prefixValid = true;

  const flushCompleteSentences = () => {
    while (prefixValid) {
      const pending = buffer.slice(emittedUpTo);
      const match = SENTENCE_BOUNDARY.exec(pending);
      if (match === null) {
        return;
      }
      const end = match.index + match[0].length;
      emit(pending.slice(0, end));
      emittedUpTo += end;
    }
  };

  return {
    push(delta: string) {
      if (delta.length === 0) {
        return;
      }
      buffer += delta;
      flushCompleteSentences();
    },
    restart() {
      prefixValid = false;
      onRestart?.();
    },
    reset() {
      buffer = "";
      emittedUpTo = 0;
      prefixValid = true;
    },
    tail(finalText: string) {
      if (!prefixValid || emittedUpTo === 0) {
        return finalText;
      }
      const prefix = buffer.slice(0, emittedUpTo);
      // Only trust the prefix if the settled text really starts with what was
      // streamed; otherwise fall back to speaking the whole settled reply.
      return finalText.startsWith(prefix)
        ? finalText.slice(prefix.length)
        : finalText;
    },
  };
}
